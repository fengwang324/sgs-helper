"""查询层测试：技能版本显式绑定与回退、检索、各类报告、按名称自动识别。

新模型下**没有** ``as_of`` / 生效日期：``skill_report`` / ``general_report`` 不再接受它，
版本由 ``GeneralVariant.skills[].version_id`` 决定，为空时按 ``mode`` 回退到唯一一条版本。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from sgs_helper.repo import (
    card_report,
    faq_report,
    faq_search,
    general_report,
    get_meta,
    keyword_report,
    mentions,
    name_index,
    product_report,
    resolve,
    search,
    skill_report,
    stats,
    timing_report,
)


def _group(report: dict, mode: str) -> dict:
    return next(g for g in report["modes"] if g["mode"] == mode)


def _version(report: dict, version_id: str) -> dict:
    return next(v for v in report["versions"] if v["id"] == version_id)


def _skill_entry(report: dict, variant_label: str, position: int = 0) -> dict:
    variant = next(v for v in report["variants"] if v["variant"] == variant_label)
    return variant["skills"][position]


def _dataset_with_versions(
    tmp_path: Path, variant_mode: str, versions: list[tuple[str, str]]
) -> Path:
    """搭一个最小数据集：一个武将版本引用一个技能，技能版本由 ``(id, mode)`` 指定。"""
    from conftest import SOURCE_YAML, write_dataset

    files = {
        "products/p.yaml": "id: product_p\nname: 演示产品\n",
        "generals/g.yaml": "id: general_g\nname: 演示武将\n",
        "general_variants/v.yaml": (
            "id: variant_v\ngeneral_id: general_g\nvariant: 标准版\n"
            f"mode: {variant_mode}\nfaction: 群\nhp: 3\n"
            "skills:\n  - skill_id: skill_s\n"
        ),
        "skills/s.yaml": "id: skill_s\nname: 演示技能本体\n",
    }
    for version_id, mode in versions:
        files[f"skill_versions/{version_id}.yaml"] = (
            f"id: {version_id}\nskill_id: skill_s\nmode: {mode}\n"
            f"text: {version_id} 的文本\n{SOURCE_YAML}verification_status: unverified\n"
        )
    return write_dataset(tmp_path / "data", files)


# ---------------------------------------------------------------- 技能版本


def test_as_of_parameter_is_gone() -> None:
    """技能版本不再按日期推导：报告函数没有 as_of 参数。"""
    assert "as_of" not in inspect.signature(skill_report).parameters
    assert "as_of" not in inspect.signature(general_report).parameters


def test_skill_report_keeps_every_mode_and_version(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo")
    assert report is not None
    assert [g["mode"] for g in report["modes"]] == ["guozhan", "identity"]
    assert _group(report, "guozhan")["version_ids"] == ["skill_demo_guozhan_v1"]
    assert _group(report, "identity")["version_ids"] == [
        "skill_demo_identity_v1",
        "skill_demo_identity_v2",
    ]
    texts = {v["mode"]: v["text"] for v in report["versions"]}
    assert texts["guozhan"] != texts["identity"]
    assert all(v["text"] for v in report["versions"])


def test_version_chain_and_review_note(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo")
    assert report is not None
    v2 = _version(report, "skill_demo_identity_v2")
    assert v2["previous_version_id"] == "skill_demo_identity_v1"
    assert v2["previous_version"]["id"] == "skill_demo_identity_v1"
    assert v2["review_note"] == "演示用待复核说明"
    assert ["skill_demo_identity_v2", "skill_demo_identity_v1"] in report["history_chains"]
    assert "change_note" not in v2


def test_explicit_version_binding_wins_over_mode(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_alpha")
    assert report is not None
    # 标准版显式绑定 v1：即使身份局还有二代版本，也用绑定的这条
    standard = _skill_entry(report, "标准版")
    assert standard["version_id"] == "skill_demo_identity_v1"
    assert standard["pinned"] is True
    assert standard["version"]["id"] == "skill_demo_identity_v1"
    assert "身份局一代" in standard["version"]["text"]


def test_empty_version_id_falls_back_to_unique_mode_version(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_alpha")
    assert report is not None
    # 国战版没写 version_id：该技能在 guozhan 下只有一条版本 -> 回退
    guozhan = _skill_entry(report, "国战版")
    assert guozhan["version_id"] is None
    assert guozhan["pinned"] is False
    assert guozhan["version"]["id"] == "skill_demo_guozhan_v1"
    assert "国战版" in guozhan["version"]["text"]


def test_ambiguous_mode_fallback_gives_no_text(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_beta")
    assert report is not None
    # 身份局有 v1 / v2 两条版本，回退匹配不唯一 -> 没有文本
    entry = _skill_entry(report, "标准版")
    assert entry["version_id"] is None
    assert entry["pinned"] is False
    assert entry["version"] is None


def test_common_version_is_the_last_resort(tmp_path: Path) -> None:
    """回退顺序：显式绑定 → 同 mode 唯一一条 → common 唯一一条 → 无文本。"""
    from sgs_helper.db import build_database
    from sgs_helper.repo import connect

    data = _dataset_with_versions(tmp_path, "ol", [("skill_s_common", "common")])
    db_path = tmp_path / "common.db"
    build_database(data, db_path)
    conn = connect(db_path)
    try:
        entry = general_report(conn, "general_g")["variants"][0]["skills"][0]
        assert entry["version_id"] is None
        assert entry["pinned"] is False
        assert entry["version"]["id"] == "skill_s_common"
    finally:
        conn.close()


def test_same_mode_version_is_preferred_over_common(tmp_path: Path) -> None:
    from sgs_helper.db import build_database
    from sgs_helper.repo import connect

    data = _dataset_with_versions(
        tmp_path, "guozhan", [("skill_s_common", "common"), ("skill_s_guozhan", "guozhan")]
    )
    db_path = tmp_path / "prefer.db"
    build_database(data, db_path)
    conn = connect(db_path)
    try:
        entry = general_report(conn, "general_g")["variants"][0]["skills"][0]
        assert entry["version"]["id"] == "skill_s_guozhan"
    finally:
        conn.close()


def test_ambiguous_common_versions_give_no_text(tmp_path: Path) -> None:
    """common 有多条并列同样算“没有文本”，不静默挑一个。"""
    from sgs_helper.db import build_database
    from sgs_helper.repo import connect

    data = _dataset_with_versions(
        tmp_path, "ol", [("skill_s_common_a", "common"), ("skill_s_common_b", "common")]
    )
    db_path = tmp_path / "ambiguous.db"
    build_database(data, db_path)
    conn = connect(db_path)
    try:
        entry = general_report(conn, "general_g")["variants"][0]["skills"][0]
        assert entry["version_id"] is None
        assert entry["pinned"] is False
        assert entry["version"] is None
    finally:
        conn.close()


def test_general_report_lists_variants_and_product(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_alpha")
    assert report is not None
    variants = {v["variant"]: v for v in report["variants"]}
    assert set(variants) == {"国战版", "标准版"}
    assert variants["国战版"]["product_id"] == "product_demo_a"
    assert report["product"]["id"] == "product_demo_a"


# ---------------------------------------------------------------- 解析 / 检索


def test_resolve_by_id_and_official_name(synth_conn) -> None:
    assert resolve(synth_conn, "skill", "skill_demo")[0]["id"] == "skill_demo"
    assert resolve(synth_conn, "skill", "演示技能")[0]["id"] == "skill_demo"
    assert resolve(synth_conn, "keyword", "演示关键词")[0]["id"] == "keyword_demo"
    assert resolve(synth_conn, "general", "演示武将甲")[0]["id"] == "general_demo_alpha"


def test_alias_is_gone_so_abbreviation_only_returns_candidates(synth_conn) -> None:
    """别名已彻底移除：缩写「演示技」不再是任何实体的官方名。"""
    entity, candidates = resolve(synth_conn, "skill", "演示技")
    assert entity is None
    assert {c["id"] for c in candidates} == {"skill_demo", "skill_demo_other"}


def test_resolve_unknown_returns_candidates(synth_conn) -> None:
    entity, candidates = resolve(synth_conn, "skill", "完全不存在的技能")
    assert entity is None
    assert candidates == []


def test_resolve_fuzzy_finds_partial_name(synth_conn) -> None:
    entity, candidates = resolve(synth_conn, "skill", "另一个")
    assert entity is not None and entity["id"] == "skill_demo_other"
    assert candidates == []


def test_search_matches_name_prefix_and_body(synth_conn) -> None:
    by_prefix = search(synth_conn, "演示技", kinds=["skill"])
    assert [item["id"] for item in by_prefix] == ["skill_demo", "skill_demo_other"]

    # 技能正文是构建期生成的（原 normalized_text），按文本能搜到
    by_body = search(synth_conn, "身份局二代", kinds=["skill_version"])
    assert [item["id"] for item in by_body] == ["skill_demo_identity_v2"]

    cross = search(synth_conn, "演示武将甲")
    assert any(item["id"] == "general_demo_alpha" for item in cross)


def test_search_cannot_find_by_removed_alias(synth_conn) -> None:
    assert search(synth_conn, "演示甲") == []


def test_search_ranks_exact_name_first(synth_conn) -> None:
    results = search(synth_conn, "演示技能", kinds=["skill", "skill_version"])
    assert results[0]["id"] == "skill_demo"
    assert results[0]["score"] == 0


def test_fts_fallback_handles_split_cjk(synth_conn) -> None:
    if get_meta(synth_conn).get("fts5") != "1":
        return  # 环境没有 FTS5 时跳过
    # LIKE 匹配不到（中间有空格），只能靠 FTS5 的分词兜底
    assert search(synth_conn, "演 示", kinds=["skill"])
    assert search(synth_conn, "演 示 技", kinds=["skill"])


def test_faq_search_by_question_and_detected_relation(synth_conn) -> None:
    assert [r["faq"]["id"] for r in faq_search(synth_conn, "演示问题")] == ["faq_demo_1"]
    # 关联对象不落库：构建期把识别到的名称拼进正文，所以按关联名也能搜到 FAQ
    assert [r["faq"]["id"] for r in faq_search(synth_conn, "演示卡牌")] == ["faq_demo_1"]
    assert faq_search(synth_conn, "完全无关的内容") == []


# ---------------------------------------------------------------- 按名称自动识别


def test_mentions_are_matched_by_official_name(synth_conn) -> None:
    hits = mentions(synth_conn, "另一个演示技能与演示技能、演示卡牌一起结算。")
    # 最长优先：另一个演示技能(7) > 演示卡牌 / 演示技能(4，同名长度按名称排序)
    assert [h["name"] for h in hits] == ["另一个演示技能", "演示卡牌", "演示技能"]
    assert {h["kind"] for h in hits} == {"skill", "card"}
    assert hits[0]["id"] == "skill_demo_other"
    assert hits[1]["id"] == "card_demo"
    assert hits[2]["id"] == "skill_demo"


def test_mentions_respects_limit_kinds_and_empty_text(synth_conn) -> None:
    assert mentions(synth_conn, "另一个演示技能与演示技能", limit=1) == [
        {"kind": "skill", "id": "skill_demo_other", "name": "另一个演示技能"}
    ]
    assert mentions(synth_conn, "演示技能", kinds=("card",)) == []
    assert mentions(synth_conn, "") == []


def test_name_index_only_contains_official_names(synth_conn) -> None:
    index = name_index(synth_conn)
    assert index["演示技能"] == ("skill", "skill_demo")
    assert index["演示卡牌"] == ("card", "card_demo")
    assert "演示甲" not in index  # 别名已彻底移除
    assert all(len(name) >= 2 for name in index)  # 单字名称不参与识别


def test_mentions_skips_single_char_names(conn) -> None:
    """真实数据里的单字卡名（例如「杀」）不参与识别，避免把整段正文刷成链接。"""
    assert "杀" not in name_index(conn)
    assert mentions(conn, "杀") == []


# ---------------------------------------------------------------- 其它报告


def test_card_report_returns_product_and_derived_mode(synth_conn) -> None:
    report = card_report(synth_conn, "演示卡牌")
    assert report is not None
    assert report["printings"][0]["product"]["name"] == "演示产品A"
    assert report["printings"][0]["mode"] == "guozhan"  # 由 product_id 推导
    marks = {p["bottom_right_mark"] for p in report["printings"]}
    assert marks == {"君", ""}


def test_product_report_lists_related_rows(synth_conn) -> None:
    report = product_report(synth_conn, "product_demo_a")
    assert report is not None
    assert [v["id"] for v in report["variants"]] == ["variant_demo_alpha_guozhan"]
    assert report["variants"][0]["general_name"] == "演示武将甲"
    assert [p["id"] for p in report["printings"]] == ["card_demo_a_spade_1"]
    assert [v["id"] for v in report["skill_versions"]] == [
        "skill_demo_guozhan_v1",
        "skill_demo_other_v1",
    ]


def test_faq_report_groups_detected_entities(synth_conn) -> None:
    report = faq_report(synth_conn, "faq_demo_1")
    assert report is not None
    assert [e["id"] for e in report["skills"]] == ["skill_demo"]
    assert [e["id"] for e in report["cards"]] == ["card_demo"]
    assert report["generals"] == []
    assert report["keywords"] == []
    assert report["timings"] == []


def test_keyword_and_timing_reports(synth_conn) -> None:
    keyword = keyword_report(synth_conn, "演示关键词")
    assert keyword is not None
    assert keyword["keyword"]["id"] == "keyword_demo"
    # 自动识别的分组键固定存在（没有命中就是空列表）
    assert keyword["timings"] == [] and keyword["skills"] == []

    timing = timing_report(synth_conn, "演示时点")
    assert timing is not None
    assert timing["timing"]["id"] == "timing_demo"
    assert timing["keywords"] == []


def test_stats_reports_meta(synth_conn) -> None:
    payload = stats(synth_conn)
    assert payload["counts"]["skill_version"] == 4
    assert payload["meta"]["schema_version"] == "3"


def test_json_serializable(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo")
    assert report is not None
    payload = json.dumps(report, ensure_ascii=False, default=str)
    assert "skill_demo_identity_v2" in payload


def test_real_data_skill_query(data_dir: Path, conn) -> None:
    """真实 data/ 里必须至少能查到一个技能，且返回结构化结果。"""
    from sgs_helper.repo import search as search_fn

    hits = search_fn(conn, "集智", kinds=["skill"])
    assert hits
    report = skill_report(conn, hits[0]["id"])
    assert report is not None
    assert report["modes"]
    assert all(v["text"] for v in report["versions"])
