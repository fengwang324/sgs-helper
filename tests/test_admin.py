"""本地资料管理网页（``sgs_helper.admin``）测试。

覆盖需求里点名的六项：
1. 页面能启动；
2. 搜索可用；
3. 编辑 YAML 后能够重新构建数据库；
4. 非法修改会被 validate 拦截，且**不落盘**；
5. SkillVersion 新增不会覆盖旧版本；
6. 被引用实体不能直接删除。

另外补了一批“真的会毁数据”的回归测试：YAML 往返稳定性、并发冲突、CSRF、
ID 路径穿越、反向引用声明完整性。

所有测试都在 ``tmp_path`` 里的**副本**上操作，绝不碰仓库里真实的 ``data/``。
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date
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


def yaml_of(workdir: Path, kind: str, entity_id: str) -> Path:
    """定位某条记录所在的 YAML 文件。

    文件名不一定等于 id（真实数据里是 ``data/generals/kongrong_sample_guozhan.yaml``
    这样的名字），所以先在磁盘上按 id 查，查不到才按新增记录的命名约定推断。
    """
    from sgs_helper.constants import KIND_BY_NAME
    from sgs_helper.loader import load_data_dir

    if (workdir / "data").is_dir():
        raws, _issues = load_data_dir(workdir / "data")
        for raw in raws:
            if raw.kind == kind and raw.data.get("id") == entity_id:
                return workdir / "data" / raw.origin.path
    return workdir / "data" / KIND_BY_NAME[kind].dirname / f"{entity_id}.yaml"


def post(client: TestClient, url: str, **kwargs):
    """提交表单：默认**不**跟随 303 重定向，便于断言“确实发生了写盘 + 重建”。"""
    kwargs.setdefault("follow_redirects", False)
    return client.post(url, **kwargs)


# ---------------------------------------------------------------- 1. 页面能启动


def test_pages_start(client: TestClient, store) -> None:
    """首页 / 列表 / 详情 / 新增 / 编辑 / 待核验 / 说明 / 健康检查全部能打开。"""
    seed = {
        "product": "product_guozhan_sample_2024",
        "general": "general_kongrong_sample_guozhan",
        "skill": "skill_jizhi",
        "skill_version": "skill_jizhi_identity_v2",
        "card": "card_sha",
        "card_printing": "card_sha_sample_guozhan_spade_7",
        "faq": "faq_0001",
        "keyword": "keyword_use_card",
        "timing": "timing_use_card",
    }
    urls = ["/", "/search?q=集智", "/review", "/help", "/healthz"]
    for kind in seed:
        urls += [f"/list/{kind}", f"/view/{kind}/{seed[kind]}", f"/new/{kind}"]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
    # 编辑页与删除确认页
    assert client.get("/edit/faq/faq_0001").status_code == 200
    assert client.get("/delete/faq/faq_0001").status_code == 200
    assert client.get("/raw/faq/faq_0001").status_code == 200
    # 未知类型 / 未知记录
    assert client.get("/list/nope").status_code == 404
    assert client.get("/view/skill/nope").status_code == 404

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
    assert "skill_jizhi" in body
    # 结果里要有模式、来源、核验状态
    assert "未核验" in body
    assert "社区资料" in body or "官方" in body


def test_search_supports_kind_and_status_filters(client: TestClient) -> None:
    only_skill = client.get("/search?q=集智&kind=skill")
    assert only_skill.status_code == 200
    assert "skill_jizhi" in only_skill.text
    assert "skill_jizhi_identity_v1" not in only_skill.text

    only_version = client.get("/search?q=集智&kind=skill_version")
    assert "skill_jizhi_identity_v1" in only_version.text
    assert '<code class="id">skill_jizhi</code>' not in only_version.text

    empty = client.get("/search?q=集智&status=physical_verified")
    assert "没有匹配" in empty.text


def test_search_by_id_works(client: TestClient) -> None:
    response = client.get("/search?q=keyword_use_card")
    assert "keyword_use_card" in response.text


# ---------------------------------------------------------------- 3. 编辑后重建数据库


def test_edit_writes_yaml_and_rebuilds_sqlite(client: TestClient, app, workdir: Path) -> None:
    payload = form_payload(app, "faq", "faq_0001", answer="【测试】改写后的回答")
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/view/faq/faq_0001")

    # YAML 落盘
    text = yaml_of(workdir, "faq", "faq_0001").read_text(encoding="utf-8")
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
    report = faq_report(conn, "faq_0001")
    conn.close()
    assert report["faq"]["answer"] == "【测试】改写后的回答"

    # 页面本身也立即读到新值（读的是 YAML）
    page = client.get("/view/faq/faq_0001")
    assert "【测试】改写后的回答" in page.text


def test_edit_keeps_yaml_readable(client: TestClient, app, workdir: Path) -> None:
    payload = form_payload(
        app, "faq", "faq_0001", answer="第一行\n第二行\n第三行"
    )
    post(client, "/edit/faq/faq_0001", data=payload)
    text = yaml_of(workdir, "faq", "faq_0001").read_text(encoding="utf-8")
    assert "answer: |-" in text  # 多行文本用块状标量，不会被折行
    reloaded = yaml.safe_load(text)
    assert reloaded["answer"] == "第一行\n第二行\n第三行"
    # 顶部注释块保留
    assert text.lstrip().startswith("#")


def test_manual_rebuild_endpoint(client: TestClient, app, workdir: Path) -> None:
    response = post(client, "/rebuild", data={"_csrf": app.state.csrf})
    assert response.status_code == 303
    assert (workdir / "build" / "sgs.db").exists()


# ---------------------------------------------------------------- 4. 非法修改被拦截且不落盘


def test_invalid_edit_is_blocked_and_nothing_written(
    client: TestClient, app, workdir: Path
) -> None:
    path = yaml_of(workdir, "general", "general_kongrong_sample_guozhan")
    before = path.read_text(encoding="utf-8")
    db_path = workdir / "build" / "sgs.db"
    build_database(workdir / "data", db_path)
    db_before = db_path.read_bytes()

    payload = form_payload(
        app, "general", "general_kongrong_sample_guozhan", skill_ids="skill_does_not_exist"
    )
    response = post(client, "/edit/general/general_kongrong_sample_guozhan", data=payload)

    assert response.status_code == 400
    assert "skill_does_not_exist" in response.text
    # 具体文件 / 字段 / 原因
    assert "data/generals/" in response.text
    assert "skill_ids" in response.text
    assert "不存在" in response.text
    # 一个字都没写，数据库也没变
    assert path.read_text(encoding="utf-8") == before
    assert db_path.read_bytes() == db_before


def test_invalid_model_field_is_blocked(client: TestClient, app, workdir: Path) -> None:
    payload = form_payload(app, "general", "general_kongrong_sample_guozhan", hp="abc")
    payload["hp"] = "abc"
    response = post(client, "/edit/general/general_kongrong_sample_guozhan", data=payload)
    assert response.status_code == 400
    assert "必须是数字" in response.text


def test_unknown_field_cannot_be_injected(client: TestClient, app, workdir: Path) -> None:
    """extra="forbid"：模型外的字段进不来（表单不会渲染它，也不会被写进 YAML）。"""
    payload = form_payload(app, "general", "general_kongrong_sample_guozhan")
    payload["totally_bogus"] = "1"
    response = post(client, "/edit/general/general_kongrong_sample_guozhan", data=payload)
    assert response.status_code == 303
    text = yaml_of(workdir, "general", "general_kongrong_sample_guozhan").read_text(encoding="utf-8")
    assert "totally_bogus" not in text


def test_duplicate_id_is_blocked(client: TestClient, app, workdir: Path) -> None:
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "keyword_use_card",
        "name": "重复 id 测试",
        "definition": "测试",
        "mode": "common",
        "aliases": "",
        "related_keyword_ids": "",
        "related_timing_ids": "",
        "source.type": "community_db",
        "source.title": "测试",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "unverified",
        "notes": "",
    }
    response = post(client, "/new/keyword", data=payload)
    assert response.status_code == 400
    assert "id 重复" in response.text


def test_bad_id_is_rejected_without_touching_disk(
    client: TestClient, app, workdir: Path
) -> None:
    payload = form_payload(app, "faq", "faq_0001")
    payload["id"] = "../../etc/passwd"
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 400
    assert "ID 只能" in response.text
    assert not (workdir / "etc").exists()
    assert not Path("/etc/passwd.yaml").exists()


# ---------------------------------------------------------------- 5. SkillVersion 不覆盖旧版本


def test_new_skill_version_keeps_history(client: TestClient, app, workdir: Path) -> None:
    old_path = yaml_of(workdir, "skill_version", "skill_jizhi_identity_v2")
    old_before = old_path.read_text(encoding="utf-8")

    today = date.today().isoformat()
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "skill_jizhi_identity_v3",
        "skill_id": "skill_jizhi",
        "mode": "identity",
        "text": "【测试】第三次调整后的技能文本",
        "version_label": "测试新版",
        "product_id": "",
        "package": "",
        "effective_from": today,
        "effective_to": "",
        "previous_version_id": "skill_jizhi_identity_v2",
        "change_note": "【测试】新增版本，旧版本保留",
        "related_keywords": "keyword_use_card",
        "related_timing_ids": "",
        "source.type": "official_rule",
        "source.title": "【测试】公告",
        "source.reference": "https://example.com/notice",
        "source.notes": "",
        "verification_status": "official",
        "notes": "",
    }
    response = post(client, "/new/skill_version", data=payload)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/view/skill_version/skill_jizhi_identity_v3")

    # 旧版本原样保留
    assert old_path.read_text(encoding="utf-8") == old_before
    new_path = yaml_of(workdir, "skill_version", "skill_jizhi_identity_v3")
    assert new_path.exists()

    # 数据库里现在是 5 条版本（原有 4 条 + 新增 1 条）
    db_path = workdir / "build" / "sgs.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    reports = skill_report(conn, "skill_jizhi")
    conn.close()
    assert len(reports["versions"]) == 5
    ids = {v["id"] for v in reports["versions"]}
    assert {"skill_jizhi_identity_v1", "skill_jizhi_identity_v2", "skill_jizhi_identity_v3"} <= ids
    if today >= "2025-06-01":  # v2 自 2025-06-01 起生效
        current_ids = [
            v["id"] for v in reports["versions"] if "identity" in v["current_for_modes"]
        ]
        assert current_ids == ["skill_jizhi_identity_v3"]

    # 技能详情页显示历史版本链
    page = client.get("/view/skill/skill_jizhi")
    assert "skill_jizhi_identity_v2" in page.text
    assert "skill_jizhi_identity_v3" in page.text


def test_new_skill_version_prefilled_from_current(client: TestClient) -> None:
    response = client.get(
        "/new/skill_version?skill_id=skill_jizhi&mode=identity"
        "&previous_version_id=skill_jizhi_identity_v2"
    )
    assert response.status_code == 200
    assert 'name="skill_id" value="skill_jizhi"' in response.text
    assert "skill_jizhi_identity_v2" in response.text


# ---------------------------------------------------------------- 6. 被引用实体不能删除


def test_delete_blocked_when_referenced(client: TestClient, app, workdir: Path) -> None:
    path = yaml_of(workdir, "product", "product_guozhan_sample_2024")
    before = path.read_text(encoding="utf-8")

    response = post(client, 
        "/delete/product/product_guozhan_sample_2024",
        data={"_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("product", "product_guozhan_sample_2024")},
    )
    assert response.status_code == 200
    assert "不能直接删除" in response.text
    assert "general_kongrong_sample_guozhan" in response.text  # 列出引用它的对象
    assert path.exists()
    assert path.read_text(encoding="utf-8") == before


def test_delete_blocked_for_skill_version_with_successor(
    client: TestClient, app, workdir: Path
) -> None:
    path = yaml_of(workdir, "skill_version", "skill_jizhi_identity_v1")
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": app.state.store.entity_sha("skill_version", "skill_jizhi_identity_v1"),
    }
    response = post(client, "/delete/skill_version/skill_jizhi_identity_v1", data=payload)
    assert response.status_code == 200
    assert "previous_version_id" in response.text
    assert path.exists()


def test_unreferenced_record_can_be_deleted(client: TestClient, app, workdir: Path) -> None:
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "keyword_temp_test",
        "name": "【测试】临时关键词",
        "definition": "测试用，随后删除",
        "mode": "common",
        "aliases": "",
        "related_keyword_ids": "",
        "related_timing_ids": "",
        "source.type": "discussion",
        "source.title": "测试",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "unverified",
        "notes": "",
    }
    created = post(client, "/new/keyword", data=payload)
    assert created.status_code == 303
    path = yaml_of(workdir, "keyword", "keyword_temp_test")
    assert path.exists()

    deleted = post(client, 
        "/delete/keyword/keyword_temp_test",
        data={"_csrf": app.state.csrf, "_base_sha": app.state.store.entity_sha("keyword", "keyword_temp_test")},
    )
    assert deleted.status_code == 303
    assert not path.exists()
    assert app.state.store.entity("keyword", "keyword_temp_test") is None


def test_delete_confirm_page_lists_references(client: TestClient) -> None:
    response = client.get("/delete/product/product_guozhan_sample_2024")
    assert response.status_code == 200
    assert "不能直接删除" in response.text
    assert "general_kongrong_sample_guozhan" in response.text


# ---------------------------------------------------------------- 7. 安全与并发回归


def test_post_without_csrf_token_is_rejected(client: TestClient, app, workdir: Path) -> None:
    path = yaml_of(workdir, "faq", "faq_0001")
    before = path.read_text(encoding="utf-8")
    payload = form_payload(app, "faq", "faq_0001", answer="【测试】不该写入")
    payload.pop("_csrf")
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 403
    assert path.read_text(encoding="utf-8") == before


def test_edit_form_contains_csrf_and_base_sha(client: TestClient) -> None:
    body = client.get("/edit/faq/faq_0001").text
    assert 'name="_csrf"' in body
    assert 'name="_base_sha"' in body


def test_post_without_base_sha_is_rejected(client: TestClient, app, workdir: Path) -> None:
    """并发校验字段不能“省略即放过”——否则任何脚本都能覆盖页面打开后的修改。"""
    path = yaml_of(workdir, "faq", "faq_0001")
    before = path.read_text(encoding="utf-8")
    payload = form_payload(app, "faq", "faq_0001", answer="【测试】不该写入")
    payload.pop("_base_sha")
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 400
    assert "_base_sha" in response.text
    assert path.read_text(encoding="utf-8") == before


def test_rollback_restores_file_when_untouched(
    client: TestClient, app, workdir: Path, monkeypatch
) -> None:
    """重建失败且文件没被别人动过：回滚到原内容。"""
    path = yaml_of(workdir, "faq", "faq_0001")
    before = path.read_text(encoding="utf-8")
    payload = form_payload(app, "faq", "faq_0001", answer="【测试】会被回滚")

    def boom():
        raise RuntimeError("模拟重建失败")

    monkeypatch.setattr(app.state.store, "_build_atomic", boom)
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 400
    assert "已回滚" in response.text
    assert path.read_text(encoding="utf-8") == before


def test_rollback_does_not_clobber_external_edit(
    client: TestClient, app, workdir: Path, monkeypatch
) -> None:
    """重建失败 + 期间有人改了同一个文件：绝不回滚覆盖别人的修改。"""
    path = yaml_of(workdir, "faq", "faq_0001")
    payload = form_payload(app, "faq", "faq_0001", answer="【测试】失败保存")

    def failing_build_with_external_edit():
        path.write_text(
            path.read_text(encoding="utf-8") + "\n# 外部追加的注释\n", encoding="utf-8"
        )
        raise RuntimeError("模拟重建失败")

    monkeypatch.setattr(
        app.state.store, "_build_atomic", failing_build_with_external_edit
    )
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 400
    text = path.read_text(encoding="utf-8")
    assert "外部追加的注释" in text   # 别人的改动保住了
    assert "【测试】失败保存" in text  # 我们写的内容也没被回滚（因为文件已被改动）
    assert "没有回滚" in response.text


def test_concurrent_modification_is_detected(client: TestClient, app, workdir: Path) -> None:
    """页面打开后文件被别人改过：必须拒绝写入，而不是覆盖。"""
    payload = form_payload(app, "faq", "faq_0001", answer="【测试】来自旧页面")
    path = yaml_of(workdir, "faq", "faq_0001")
    external = path.read_text(encoding="utf-8").replace("占位数据", "别人改过的内容")
    path.write_text(external, encoding="utf-8")

    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 400
    assert "没有写入任何文件" in response.text
    assert "别人改过的内容" in path.read_text(encoding="utf-8")
    assert "【测试】来自旧页面" not in path.read_text(encoding="utf-8")


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
        "- id: keyword_a\n  name: 甲\n  definition: 甲的说明\n  mode: common\n"
        "  source:\n    type: wiki\n    title: 来源\n    reference: ''\n    notes: ''\n"
        "  verification_status: unverified\n  notes: ''\n"
        "- id: keyword_b\n  name: 乙\n  definition: 乙的说明\n  mode: common\n"
        "  source:\n    type: wiki\n    title: 来源\n    reference: ''\n    notes: ''\n"
        "  verification_status: unverified\n  notes: ''\n",
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
            "mode": "common",
            "source": {"type": "wiki", "title": "来源"},
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
        "id: keyword_solo\nname: 单条\ndefinition: 说明\nmode: common\n", encoding="utf-8"
    )
    assert yaml_writer.remove_entity(path, "keyword_solo") is True
    assert not path.exists()


# ---------------------------------------------------------------- 9. 声明完整性


def test_reference_fields_cover_every_id_field() -> None:
    """任何 ``*_id`` / ``*_ids`` 模型字段都必须在 REFERENCE_FIELDS 里声明。

    新增引用字段却忘了更新声明表时，删除保护会静默失效 —— 这条测试就是为了
    让那种情况在 CI 里立刻炸出来。
    """
    declared = {(kind, field) for kind, field, _target, _is_list in REFERENCE_FIELDS}
    missing = []
    for kind, model in MODEL_BY_KIND.items():
        for name in model.model_fields:
            if name == "id":
                continue
            if name.endswith("_id") or name.endswith("_ids"):
                if (kind, name) not in declared:
                    missing.append((kind, name))
    assert missing == [], f"这些引用字段没有在 REFERENCE_FIELDS 里声明：{missing}"


def test_reverse_refs_and_delete_guard_agree(app) -> None:
    store = app.state.store
    refs = store.reverse_refs("skill", "skill_jizhi")
    kinds = {ref["kind"] for ref in refs}
    assert "general" in kinds
    assert "skill_version" in kinds
    outcome = store.delete("skill", "skill_jizhi")
    assert outcome.ok is False
    assert outcome.blocked is True


# ---------------------------------------------------------------- 10. 待核验页面


def test_review_page_groups_by_verification_status(app, client: TestClient) -> None:
    store = app.state.store
    groups = {group["status"]: group for group in store.review_groups()}
    assert set(groups) == {"unverified", "community", "conflict"}

    body = client.get("/review").text
    assert "待核验清单" in body
    for group in groups.values():
        assert f"{group['count']} 条" in body

    only_conflict = client.get("/review?status=conflict")
    assert only_conflict.status_code == 200
    assert "来源冲突" in only_conflict.text
    assert "未核验</span>" not in only_conflict.text


# ---------------------------------------------------------------- 11. 新增（表单 -> YAML -> DB）


def test_create_general_from_form(client: TestClient, app, workdir: Path) -> None:
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "general_test_new",
        "name": "【测试】新武将",
        "mode": "guozhan",
        "faction": "群",
        "hp": "2.5",
        "gender": "male",
        "product_id": "product_guozhan_sample_2024",
        "package": "test",
        "skill_ids": "skill_jizhi",
        "aliases": "测试别名\n另一个别名",
        "source.type": "physical_card",
        "source.title": "本人实体卡",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "physical_verified",
        "notes": "",
    }
    response = post(client, "/new/general", data=payload)
    assert response.status_code == 303, response.text

    text = yaml_of(workdir, "general", "general_test_new").read_text(encoding="utf-8")
    reloaded = yaml.safe_load(text)
    assert reloaded["hp"] == 2.5
    assert reloaded["aliases"] == ["测试别名", "另一个别名"]
    assert reloaded["skill_ids"] == ["skill_jizhi"]
    assert reloaded["verification_status"] == "physical_verified"

    db_path = workdir / "build" / "sgs.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = general_report(conn, "general_test_new")
    conn.close()
    assert report["general"]["hp"] == 2.5
    assert report["general"]["aliases"] == ["测试别名", "另一个别名"]
    assert report["product"]["id"] == "product_guozhan_sample_2024"


def test_create_general_without_source_uses_model_default(client: TestClient, app, workdir: Path) -> None:
    """source 可选的实体：全部留空就写 null，而不是空结构。"""
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "general_test_nosource",
        "name": "【测试】无来源武将",
        "mode": "identity",
        "faction": "",
        "hp": "3",
        "gender": "unknown",
        "product_id": "",
        "package": "",
        "skill_ids": "",
        "aliases": "",
        "source.type": "",
        "source.title": "",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "unverified",
        "notes": "",
    }
    response = post(client, "/new/general", data=payload)
    assert response.status_code == 303, response.text
    reloaded = yaml.safe_load(yaml_of(workdir, "general", "general_test_nosource").read_text(encoding="utf-8"))
    assert reloaded["source"] is None
    assert reloaded["skill_ids"] == []


def test_source_required_entity_needs_source(client: TestClient, app, workdir: Path) -> None:
    payload = {
        "_csrf": app.state.csrf,
        "_base_sha": "",
        "id": "keyword_test_nosource",
        "name": "【测试】缺来源关键词",
        "definition": "测试",
        "mode": "common",
        "aliases": "",
        "related_keyword_ids": "",
        "related_timing_ids": "",
        "source.type": "",
        "source.title": "",
        "source.reference": "",
        "source.notes": "",
        "verification_status": "unverified",
        "notes": "",
    }
    response = post(client, "/new/keyword", data=payload)
    assert response.status_code == 400
    assert "来源类型必填" in response.text
    assert not yaml_of(workdir, "keyword", "keyword_test_nosource").exists()


def test_edit_source_reference_url_is_clickable(client: TestClient, app) -> None:
    payload = form_payload(app, "faq", "faq_0001", **{"source.reference": "https://example.com/faq"})
    response = post(client, "/edit/faq/faq_0001", data=payload)
    assert response.status_code == 303
    page = client.get("/view/faq/faq_0001")
    assert 'href="https://example.com/faq"' in page.text
    assert 'rel="noopener noreferrer"' in page.text


# ---------------------------------------------------------------- 12. 列表筛选


def test_list_filters(client: TestClient) -> None:
    assert client.get("/list/product?status=unverified").status_code == 200
    assert client.get("/list/skill_version?mode=identity").status_code == 200
    assert client.get("/list/card_printing?product=product_guozhan_sample_2024").status_code == 200
    filtered = client.get("/list/card_printing?product=product_guozhan_sample_2024")
    assert "product_guozhan_sample_2024" in filtered.text
    empty = client.get("/list/card_printing?product=product_nonexistent")
    assert "没有符合条件" in empty.text
