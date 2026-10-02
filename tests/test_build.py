"""SQLite 构建测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import replace_in

from sgs_helper.db import DataValidationError, build_database
from sgs_helper.loader import manifest_digest
from sgs_helper.repo import get_meta

EXPECTED_TABLES = {
    "products",
    "generals",
    "general_skills",
    "skills",
    "skill_versions",
    "cards",
    "card_printings",
    "faqs",
    "keywords",
    "timings",
    "sources",
    "search_index",
    "meta",
}


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        return {row[0] for row in rows}
    finally:
        conn.close()


def test_all_expected_tables_created(synth_db: Path) -> None:
    assert EXPECTED_TABLES <= _tables(synth_db)


def test_build_creates_db_file(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "sgs.db"
    result = build_database(synth, db_path)
    assert db_path.exists()
    assert result.total == 18
    assert result.db_path == db_path


def test_counts_match_bundle(synth: Path, tmp_path: Path) -> None:
    result = build_database(synth, tmp_path / "sgs.db")
    assert result.counts["product"] == 2
    assert result.counts["general"] == 2
    assert result.counts["skill"] == 2
    assert result.counts["skill_version"] == 4
    assert result.counts["card"] == 1
    assert result.counts["card_printing"] == 2
    assert result.counts["faq"] == 1
    assert result.counts["keyword"] == 2
    assert result.counts["timing"] == 2
    assert result.total == 18


def test_rebuild_is_idempotent(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    first = build_database(synth, db_path)
    conn = sqlite3.connect(db_path)
    rows_first = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(EXPECTED_TABLES)]
    conn.close()

    second = build_database(synth, db_path)
    conn = sqlite3.connect(db_path)
    rows_second = [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(EXPECTED_TABLES)]
    conn.close()

    assert first.counts == second.counts
    assert rows_first == rows_second
    assert rows_second[sorted(EXPECTED_TABLES).index("skill_versions")] == 4


def test_build_refuses_invalid_data(synth: Path, tmp_path: Path) -> None:
    replace_in(synth / "generals" / "demo_alpha.yaml", "skill_id: skill_demo", "skill_id: skill_missing")
    with pytest.raises(DataValidationError) as excinfo:
        build_database(synth, tmp_path / "sgs.db")
    assert "skill_missing" in excinfo.value.result.format_report()


def test_build_can_force_invalid_data(synth: Path, tmp_path: Path) -> None:
    replace_in(synth / "generals" / "demo_alpha.yaml", "skill_id: skill_demo", "skill_id: skill_missing")
    db_path = tmp_path / "sgs.db"
    build_database(synth, db_path, allow_invalid=True)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM generals").fetchone()[0] == 2
    finally:
        conn.close()


def test_sources_are_deduplicated(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path)
    assert result.source_count == 1  # 全部指向同一份 source
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        linked = conn.execute(
            "SELECT COUNT(*) FROM skill_versions WHERE source_key IS NOT NULL"
        ).fetchone()[0]
        assert linked == 4
    finally:
        conn.close()


def test_meta_records_traceability(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    build_database(synth, db_path)
    meta = get_meta(sqlite3.connect(db_path))
    assert meta["schema_version"] == "1"
    assert meta["yaml_manifest_sha256"] == manifest_digest(synth)
    assert meta["count.skill_version"] == "4"
    assert meta["entity_total"] == "18"
    assert meta["fts5"] in {"0", "1"}
    assert "built_at" in meta


def test_general_skills_join_table(synth_db: Path) -> None:
    conn = sqlite3.connect(synth_db)
    try:
        rows = conn.execute(
            "SELECT general_id, skill_id, version_id, position FROM general_skills "
            "ORDER BY general_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("general_demo_alpha", "skill_demo", None, 0),
        ("general_demo_beta", "skill_demo", None, 0),
    ]


def test_search_index_covers_every_entity(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path)
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
        assert count == result.total
        # 技能版本的可读名称里带上了技能名，便于检索结果显示
        name = conn.execute(
            "SELECT name FROM search_index WHERE kind = 'skill_version' "
            "AND entity_id = 'skill_demo_identity_v2'"
        ).fetchone()[0]
        assert "演示技能" in name
    finally:
        conn.close()


def test_fts_index_created_when_available(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path, with_fts=True)
    conn = sqlite3.connect(db_path)
    try:
        meta = get_meta(conn)
        if result.with_fts:
            assert meta["fts5"] == "1"
            assert conn.execute("SELECT COUNT(*) FROM fts_search").fetchone()[0] == result.total
        else:  # pragma: no cover - 本环境有 FTS5
            assert meta["fts5"] == "0"
    finally:
        conn.close()


def test_fts_can_be_disabled(synth: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "sgs.db"
    result = build_database(synth, db_path, with_fts=False)
    assert result.with_fts is False
    conn = sqlite3.connect(db_path)
    try:
        assert get_meta(conn)["fts5"] == "0"
        assert "fts_search" not in _tables(db_path)
    finally:
        conn.close()


def test_trace_columns_are_filled(synth_db: Path) -> None:
    conn = sqlite3.connect(synth_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT yaml_file, yaml_line FROM skill_versions WHERE id = 'skill_demo_identity_v2'"
        ).fetchone()
    finally:
        conn.close()
    assert row["yaml_file"] == "data/skill_versions/demo_identity_v2.yaml"
    assert row["yaml_line"] >= 1


def test_real_data_builds_and_has_rows(data_dir: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "real.db"
    result = build_database(data_dir, db_path)
    assert result.total == sum(result.counts.values())
    assert result.counts["skill_version"] >= 1
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0] == result.total
    finally:
        conn.close()
