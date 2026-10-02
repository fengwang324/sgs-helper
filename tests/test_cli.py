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


@pytest.fixture(scope="module")
def cli_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db_path = tmp_path_factory.mktemp("cli") / "sgs.db"
    result = run_script("scripts/build_db.py", "--db", str(db_path))
    assert result.returncode == 0, result.stderr
    return db_path


# ---------------------------------------------------------------- validate


def test_validate_script_passes() -> None:
    result = run_script("scripts/validate_data.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "校验通过" in result.stdout


def test_validate_script_reports_error(tmp_path: Path, data_dir: Path) -> None:
    broken = tmp_path / "data"
    shutil.copytree(data_dir, broken)
    replace_in(
        broken / "generals" / "kongrong_sample_guozhan.yaml", "- skill_lirang",
        "- skill_not_exist",
    )
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


def test_build_script_refuses_invalid_data(tmp_path: Path, data_dir: Path) -> None:
    broken = tmp_path / "data"
    shutil.copytree(data_dir, broken)
    replace_in(
        broken / "card_printings" / "sha_sample_guozhan_spade_7.yaml",
        "card_id: card_sha",
        "card_id: card_not_exist",
    )
    result = run_script(
        "scripts/build_db.py", "--data-dir", str(broken), "--db", str(tmp_path / "x.db")
    )
    assert result.returncode == 2
    assert "card_not_exist" in result.stderr


# ---------------------------------------------------------------- query


@pytest.mark.parametrize(
    ("command", "query"),
    [
        ("skill", "集智"),
        ("general", "孔融"),
        ("card", "杀"),
        ("faq", "伤害"),
        ("keyword", "濒死"),
        ("timing", "伤害结算"),
        ("product", "国战"),
        ("search", "集智"),
    ],
)
def test_spec_queries_work(cli_db: Path, command: str, query: str) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), command, query)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip()
    assert "Traceback" not in result.stderr


def test_query_skill_shows_versions_and_source(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "skill", "集智")
    assert result.returncode == 0
    for token in ["当前有效版本", "技能文本", "来源", "校验状态", "国战", "身份局"]:
        assert token in result.stdout


def test_query_json_output(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "--json", "skill", "集智")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["definition"]["id"] == "skill_jizhi"
    assert payload["modes"]


def test_query_as_of_switches_version(cli_db: Path) -> None:
    # 选项写在子命令之后也要能用
    result = run_script(
        "scripts/query.py", "--db", str(cli_db), "skill", "集智",
        "--as-of", "2024-01-01", "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    identity = next(g for g in payload["modes"] if g["mode"] == "identity")
    assert identity["current_id"] == "skill_jizhi_identity_v1"

    # 选项写在子命令之前同样可用
    result_before = run_script(
        "scripts/query.py", "--db", str(cli_db), "--as-of", "2026-10-02", "--json",
        "skill", "集智",
    )
    assert result_before.returncode == 0
    payload_before = json.loads(result_before.stdout)
    identity_before = next(g for g in payload_before["modes"] if g["mode"] == "identity")
    assert identity_before["current_id"] == "skill_jizhi_identity_v2"


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


def test_query_card_shows_printings_and_mark(cli_db: Path) -> None:
    result = run_script("scripts/query.py", "--db", str(cli_db), "card", "杀")
    assert result.returncode == 0
    assert "右下角角标" in result.stdout
    assert "实体卡印刷" in result.stdout
