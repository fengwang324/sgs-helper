"""查询层测试：当前有效版本推导、检索、各类报告。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sgs_helper.repo import (
    card_report,
    faq_search,
    general_report,
    get_meta,
    keyword_report,
    product_report,
    resolve,
    search,
    skill_report,
    stats,
    timing_report,
)

AS_OF_2022 = dt.date(2022, 6, 1)
AS_OF_2026 = dt.date(2026, 1, 2)


def _group(report: dict, mode: str) -> dict:
    return next(g for g in report["modes"] if g["mode"] == mode)


def _version(report: dict, version_id: str) -> dict:
    return next(v for v in report["versions"] if v["id"] == version_id)


# ---------------------------------------------------------------- 版本推导


def test_current_version_follows_effective_dates(synth_conn) -> None:
    old = skill_report(synth_conn, "skill_demo", AS_OF_2022)
    new = skill_report(synth_conn, "skill_demo", AS_OF_2026)
    assert old is not None and new is not None
    assert _group(old, "identity")["current_id"] == "skill_demo_identity_v1"
    assert _group(new, "identity")["current_id"] == "skill_demo_identity_v2"


def test_current_version_is_flagged_per_version(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo", AS_OF_2026)
    assert report is not None
    assert _version(report, "skill_demo_identity_v2")["current_for_modes"] == ["identity"]
    assert _version(report, "skill_demo_identity_v1")["current_for_modes"] == []
    assert _version(report, "skill_demo_identity_v1")["status"] == "expired"
    assert _version(report, "skill_demo_guozhan_v1")["status"] == "active"
    assert _version(report, "skill_demo_guozhan_v1")["current_for_modes"] == ["guozhan"]


def test_future_version_is_not_current(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo", dt.date(2024, 6, 1))
    assert report is not None
    future = _version(report, "skill_demo_identity_v2")
    assert future["status"] == "future"
    assert _group(report, "identity")["current_id"] == "skill_demo_identity_v1"


def test_no_current_version_when_all_expired(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo", dt.date(2030, 1, 1))
    assert report is not None
    assert _group(report, "identity")["current_id"] == "skill_demo_identity_v2"


def test_history_chain(synth_conn) -> None:
    report = skill_report(synth_conn, "skill_demo", AS_OF_2026)
    assert report is not None
    assert ["skill_demo_identity_v2", "skill_demo_identity_v1"] in report["history_chains"]


def test_general_report_uses_mode_specific_version(synth_conn) -> None:
    report = general_report(synth_conn, "general_demo_alpha", AS_OF_2026)
    assert report is not None
    entry = report["skills"][0]
    assert entry["current"]["id"] == "skill_demo_guozhan_v1"
    assert "国战版" in entry["current"]["text"]


# ---------------------------------------------------------------- 解析 / 检索


def test_resolve_by_id_name_and_alias(synth_conn) -> None:
    assert resolve(synth_conn, "skill", "skill_demo")[0]["id"] == "skill_demo"
    assert resolve(synth_conn, "skill", "演示技")[0]["id"] == "skill_demo"
    assert resolve(synth_conn, "keyword", "演示词")[0]["id"] == "keyword_demo"
    assert resolve(synth_conn, "general", "演示甲")[0]["id"] == "general_demo_alpha"


def test_resolve_unknown_returns_candidates(synth_conn) -> None:
    entity, candidates = resolve(synth_conn, "skill", "完全不存在的技能")
    assert entity is None
    assert candidates == []


def test_resolve_fuzzy_finds_partial_name(synth_conn) -> None:
    entity, candidates = resolve(synth_conn, "skill", "另一个")
    assert entity is not None and entity["id"] == "skill_demo_other"


def test_search_hits_alias_and_body(synth_conn) -> None:
    by_alias = search(synth_conn, "演示技", kinds=["skill"])
    ids = [item["id"] for item in by_alias]
    assert ids[0] == "skill_demo"  # 精确别名命中排在最前
    assert "skill_demo_other" in ids

    by_body = search(synth_conn, "身份局二代", kinds=["skill_version"])
    assert [item["id"] for item in by_body] == ["skill_demo_identity_v2"]

    cross = search(synth_conn, "演示甲")
    assert any(item["id"] == "general_demo_alpha" for item in cross)


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


def test_faq_search_by_question_and_relation(synth_conn) -> None:
    assert [r["faq"]["id"] for r in faq_search(synth_conn, "演示问题")] == ["faq_demo_1"]
    assert [r["faq"]["id"] for r in faq_search(synth_conn, "演示关键词")] == ["faq_demo_1"]
    assert faq_search(synth_conn, "完全无关的内容") == []


# ---------------------------------------------------------------- 其它报告


def test_card_report_returns_product_and_mark(synth_conn) -> None:
    report = card_report(synth_conn, "演示牌")
    assert report is not None
    assert report["printings"][0]["product"]["name"] == "演示产品A"
    marks = {p["bottom_right_mark"] for p in report["printings"]}
    assert marks == {"君", ""}


def test_product_report_lists_related_rows(synth_conn) -> None:
    report = product_report(synth_conn, "product_demo_a")
    assert report is not None
    assert [g["id"] for g in report["generals"]] == ["general_demo_alpha"]
    assert [p["id"] for p in report["printings"]] == ["card_demo_a_spade_1"]
    assert [v["id"] for v in report["skill_versions"]] == [
        "skill_demo_guozhan_v1",
        "skill_demo_other_v1",
    ]


def test_keyword_and_timing_reports(synth_conn) -> None:
    keyword = keyword_report(synth_conn, "演示关键词")
    assert keyword is not None
    assert keyword["keyword"]["id"] == "keyword_demo"

    timing = timing_report(synth_conn, "演示时点")
    assert timing is not None
    assert timing["timing"]["id"] == "timing_demo"


def test_stats_reports_meta(synth_conn) -> None:
    payload = stats(synth_conn)
    assert payload["counts"]["skill_version"] == 4
    assert payload["meta"]["schema_version"] == "1"


def test_json_serializable(synth_conn) -> None:
    import json

    report = skill_report(synth_conn, "skill_demo", AS_OF_2026)
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
