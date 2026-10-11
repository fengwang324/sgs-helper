"""CLI 端到端测试：直接执行 scripts/ 下的三个脚本。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import replace_in

ROOT = Path(__file__).resolve().parents[1]


def run_script(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=str(cwd), capture_output=True, text=True
    )


def _find_yaml(directory: Path, needle: str) -> Path:
    """按内容定位真实数据文件，避免把文件名写死在测试里。"""
    for path in sorted(directory.glob("*.yaml")):
        if needle in path.read_text(encoding="utf-8"):
            return path
    raise AssertionError(f"{directory} 下找不到包含 {needle!r} 的文件")


@pytest.fixture(scope="module")
def cli_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("cli") / "sgs.db"
    result = run_script("scripts/build_db.py", "--db", str(db_path))
    assert result.returncode == 0, result.stderr
    return db_path


def _synth_db(synth: Path, tmp_path: Path) -> Path:
    from sgs_helper.db import build_database

    db_path = tmp_path / "synth.db"
    build_database(synth, db_path)
    return db_path


# ---------------------------------------------------------------- validate


def test_validate_script_passes() -> None:
    result = run_script("scripts/validate_data.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "校验通过" in result.stdout


def test_validate_script_reports_error(tmp_path: Path, data_dir: Path) -> None:
    broken = tmp_path / "data"
    shutil.copytree(data_dir, broken)
    target = _find_yaml(broken / "general_variants", "skill_id: skill_guan_xing")
    replace_in(target, "skill_id: skill_guan_xing", "skill_id: skill_not_exist")
    result = run_script("scripts/validate_data.py", "--data-dir", str(broken))
    assert result.returncode == 1
    assert "skill_not_exist" in result.stdout
    assert "校验失败" in result.stdout


def test_validate_script_quiet(tmp_path: Path) -> None:
    result = run_script("scripts/validate_data.py", "--quiet")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ---------------------------------------------------------------- build


def test_build_script_creates_database(tmp_path: Path) -> None:
    db_path = tmp_path / "out" / "sgs.db"
    result = run_script("scripts/build_db.py", "--db", str(db_path))
    assert result.returncode == 0, result.stderr
    assert "已生成数据库" in result.stdout
    assert db_path.exists()


def test_build_script_refuses_removed_card_fields(tmp_path: Path, data_dir: Path) -> None:
    broken = tmp_path / "data"
    shutil.copytree(data_dir, broken)
    target = _find_yaml(broken / "cards", "id: card_ding_lan_ye_ming_zhu")
    target.write_text(target.read_text(encoding="utf-8") + "effect_skills: []\n", encoding="utf-8")
    result = run_script(
        "scripts/build_db.py", "--data-dir", str(broken), "--db", str(tmp_path / "x.db")
    )
    assert result.returncode == 2
    assert "effect_skills" in result.stderr


# ---------------------------------------------------------------- query


@pytest.mark.parametrize(
    ("command", "query"),
    [
        ("skill", "集智"),
        ("general", "诸葛亮"),
        ("card", "杀"),
        ("faq", "伤害"),
        ("keyword", "摸牌"),
        ("product", "群狼环鼎"),
        ("search", "集智"),
    ],
)
def test_spec_queries_work(cli_db: Path, command: str, query: str) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), command, query)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip()
    assert "Traceback" not in result.stderr


def test_query_skill_shows_versions_and_product(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "skill", "集智")
    assert result.returncode == 0
    for token in ["技能文本", "国战", "群狼环鼎"]:
        assert token in result.stdout


def test_query_json_output(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "--json", "skill", "集智")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["definition"]["id"] == "skill_ji_zhi"
    assert payload["modes"]


def test_query_as_of_option_is_removed(cli_db: Path) -> None:
    """技能版本不再按日期推导：CLI 的 --as-of 已删除。"""
    result = run_script(
        "scripts/query.py", "--db", str(cli_db), "skill", "集智", "--as-of", "2024-06-01"
    )
    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr


def test_query_skill_lists_versions_per_mode(synth: Path, tmp_path: Path) -> None:
    db_path = _synth_db(synth, tmp_path)
    result = run_script("scripts/query.py", "--db", str(db_path), "--json", "skill", "演示技能")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    groups = {group["mode"]: group["version_ids"] for group in payload["modes"]}
    assert groups == {
        "guozhan": ["skill_demo_guozhan_v1"],
        "identity": ["skill_demo_identity_v1", "skill_demo_identity_v2"],
    }


def test_query_general_shows_binding_and_fallback(synth: Path, tmp_path: Path) -> None:
    """国战版没写 version_id -> 按 mode 回退；标准版显式绑定 v1。"""
    db_path = _synth_db(synth, tmp_path)
    result = run_script(
        "scripts/query.py", "--db", str(db_path), "--json", "general", "演示武将甲"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_title = {v["title"]: v["skills"][0] for v in payload["variants"]}

    assert by_title["演示称号甲"]["version_id"] is None
    assert by_title["演示称号甲"]["pinned"] is False
    assert by_title["演示称号甲"]["version"]["id"] == "skill_demo_guozhan_v1"

    assert by_title["演示标准称号"]["version_id"] == "skill_demo_identity_v1"
    assert by_title["演示标准称号"]["pinned"] is True
    assert by_title["演示标准称号"]["version"]["id"] == "skill_demo_identity_v1"


def test_query_stats(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "stats")
    assert result.returncode == 0
    assert "schema 版本" in result.stdout


def test_query_missing_database(tmp_path: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(tmp_path / "nope.db"), "skill", "集智")
    assert result.returncode == 1
    assert "请先执行" in result.stderr


def test_query_unknown_entity(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "skill", "完全不存在的技能")
    assert result.returncode == 1
    assert "没有找到" in result.stdout


def test_query_card_shows_categories_and_effect_skill(cli_db: Path) -> None:
    result = run_script(
        "scripts/query.py", "--db", str(cli_db), "card", "card_ding_lan_ye_ming_zhu"
    )
    assert result.returncode == 0
    for token in [
        "分类：游戏牌 / 装备牌 / 宝物",
        "拓展包：九变篇",
        "卡图：card_ding_lan_ye_ming_zhu_qlhd_diamond_6.jpg",
        "效果",
        "制衡",
    ]:
        assert token in result.stdout


def test_query_card_is_single_layer(cli_db: Path) -> None:
    """卡牌单层：CLI 不再渲染「实体卡印刷 / 右下角角标」这些 CardPrinting 概念。"""
    result = run_script("scripts/query.py", "--db", str(cli_db), "card", "杀")
    assert result.returncode == 0
    assert "分类：游戏牌 / 基本牌 / 杀" in result.stdout
    assert "效果：" in result.stdout
    assert "实体卡印刷" not in result.stdout
    assert "右下角角标" not in result.stdout


def test_query_card_json_is_single_layer(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "--json", "card", "card_sha")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["kind"] == "card"
    assert set(payload) == {"kind", "card", "effect", "attack_range", "deck_info", "note", "rules", "generals", "skills", "cards", "keywords"}
    assert "printings" not in payload
    assert payload["card"]["name"] == "杀"
    assert payload["card"]["category_3"] == "杀"
    assert "card_id" not in payload["card"] and "suit" not in payload["card"]
    assert payload["effect"] == ""
    assert payload["attack_range"] == ""
    assert payload["deck_info"]
    assert payload["note"] == ""
    assert payload["rules"] == {"use_methods": [
        {"timing": "出牌阶段限一次。", "target": "你的攻击范围内的一名其他角色。", "effect": "你对目标对应的角色造成1点伤害。"}
    ]}
