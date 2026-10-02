"""pytest 共享夹具。

两个数据来源：

1. ``data_dir`` / ``built_db`` / ``conn`` —— 使用仓库里真实的 ``data/``（当前是 SAMPLE 数据）；
2. ``synth`` / ``synth_db`` / ``synth_conn`` —— 测试自带的**合成数据集**，
   用固定 id 和固定日期验证“当前有效版本”等行为，不依赖真实数据的增删。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sgs_helper.db import build_database  # noqa: E402
from sgs_helper.repo import connect  # noqa: E402

DATA_DIR = ROOT / "data"

SOURCE_YAML = """source:
  type: community_db
  title: 演示来源
  reference: ""
  notes: 测试用
"""

# ---------------------------------------------------------------- 合成数据集
# 设计要点：3 条 skill_demo 版本覆盖「国战 / 身份局两代版本」，
# 身份局 v1 于 2024-12-31 过期、v2 自 2025-01-01 生效，便于验证日期推导。

SYNTHETIC: dict[str, str] = {
    "products/demo_a.yaml": f"""id: product_demo_a
name: 演示产品A
series: 演示系列
mode: guozhan
publisher: 演示
release_year: 2024
edition: 初版
general_sets:
  - 演示武将牌系列
card_sets:
  - 演示牌堆
{SOURCE_YAML}verification_status: unverified
""",
    "products/demo_b.yaml": f"""id: product_demo_b
name: 演示产品B
mode: identity
release_year: 2020
{SOURCE_YAML}verification_status: unverified
""",
    "generals/demo_alpha.yaml": f"""id: general_demo_alpha
name: 演示武将甲
mode: guozhan
faction: 群
hp: 1.5
gender: male
product_id: product_demo_a
general_set: 演示武将牌系列
skills:
  - skill_id: skill_demo
aliases:
  - 演示甲
{SOURCE_YAML}verification_status: unverified
""",
    "generals/demo_beta.yaml": f"""id: general_demo_beta
name: 演示武将乙
mode: identity
faction: 蜀
hp: 3
gender: female
product_id: product_demo_b
skills:
  - skill_id: skill_demo
{SOURCE_YAML}verification_status: unverified
""",
    "skills/demo.yaml": f"""id: skill_demo
name: 演示技能
aliases:
  - 演示技
{SOURCE_YAML}""",
    "skills/demo_other.yaml": f"""id: skill_demo_other
name: 另一个演示技能
{SOURCE_YAML}""",
    "skill_versions/demo_other_v1.yaml": f"""id: skill_demo_other_v1
skill_id: skill_demo_other
mode: guozhan
text: 另一个技能的演示文本
product_id: product_demo_a
effective_from: 2024-01-01
{SOURCE_YAML}verification_status: unverified
""",
    "skill_versions/demo_guozhan_v1.yaml": f"""id: skill_demo_guozhan_v1
skill_id: skill_demo
mode: guozhan
text: 国战版演示技能文本
version_label: 国战初版
product_id: product_demo_a
effective_from: 2024-01-01
related_keywords:
  - keyword_demo
related_timing_ids:
  - timing_demo
{SOURCE_YAML}verification_status: unverified
""",
    "skill_versions/demo_identity_v1.yaml": f"""id: skill_demo_identity_v1
skill_id: skill_demo
mode: identity
text: 身份局一代演示技能文本
version_label: 身份局初版
product_id: product_demo_b
effective_from: 2020-01-01
effective_to: 2024-12-31
{SOURCE_YAML}verification_status: unverified
""",
    "skill_versions/demo_identity_v2.yaml": f"""id: skill_demo_identity_v2
skill_id: skill_demo
mode: identity
text: 身份局二代演示技能文本
version_label: 平衡调整后
product_id: null
effective_from: 2025-01-01
previous_version_id: skill_demo_identity_v1
change_note: 演示用变更说明
{SOURCE_YAML}verification_status: unverified
""",
    "cards/demo_card.yaml": f"""id: card_demo
name: 演示卡牌
card_type: basic
subtype: normal
aliases:
  - 演示牌
related_keywords:
  - keyword_demo
""",
    "card_printings/demo_a_spade_1.yaml": f"""id: card_demo_a_spade_1
card_id: card_demo
name_printed: 演示卡牌
mode: guozhan
product_id: product_demo_a
card_set: base
suit: spade
rank: 1
printed_text: A 产品演示卡面原文
bottom_right_mark: 君
card_number: "001"
{SOURCE_YAML}verification_status: unverified
""",
    "card_printings/demo_b_heart_5.yaml": f"""id: card_demo_b_heart_5
card_id: card_demo
name_printed: 演示卡牌
mode: identity
product_id: product_demo_b
card_set: base
suit: heart
rank: 5
printed_text: B 产品演示卡面原文
{SOURCE_YAML}verification_status: unverified
""",
    "faq/demo_1.yaml": f"""id: faq_demo_1
question: 演示问题：技能如何结算？
answer: 演示答案（非真实裁定）
mode: identity
related_skill_ids:
  - skill_demo
related_card_ids:
  - card_demo
related_keywords:
  - keyword_demo
related_timing_ids:
  - timing_demo
{SOURCE_YAML}verification_status: community
""",
    "keywords/demo.yaml": f"""id: keyword_demo
name: 演示关键词
definition: 演示用规则关键词定义
aliases:
  - 演示词
related_timing_ids:
  - timing_demo
{SOURCE_YAML}verification_status: unverified
""",
    "timings/demo.yaml": f"""id: timing_demo
name: 演示时点
description: 演示用时点说明
next_ids:
  - timing_demo_next
related_keywords:
  - keyword_demo
  - keyword_demo2
related_skill_ids:
  - skill_demo
{SOURCE_YAML}verification_status: unverified
""",
    "timings/demo_next.yaml": f"""id: timing_demo_next
name: 演示后续时点
description: 演示用的后续时点
previous_ids:
  - timing_demo
{SOURCE_YAML}verification_status: unverified
""",
    "keywords/demo2.yaml": f"""id: keyword_demo2
name: 演示关键词二
definition: 第二个演示关键词
{SOURCE_YAML}verification_status: unverified
""",
}


def write_dataset(root: Path, files: dict[str, str]) -> Path:
    """把 ``{相对路径: 内容}`` 写成 data/ 目录。"""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture
def copy_of_data(tmp_path: Path) -> Path:
    """真实 data/ 的副本，测试可以随意改坏。"""
    target = tmp_path / "data"
    shutil.copytree(DATA_DIR, target)
    return target


@pytest.fixture
def synth(tmp_path: Path) -> Path:
    """合成数据集（固定 id / 固定日期）。"""
    return write_dataset(tmp_path / "data", SYNTHETIC)


@pytest.fixture(scope="session")
def built_db(tmp_path_factory: pytest.TempPathFactory, data_dir: Path) -> Path:
    db_path = tmp_path_factory.mktemp("db") / "sgs.db"
    build_database(data_dir, db_path)
    return db_path


@pytest.fixture
def conn(built_db: Path):
    connection = connect(built_db)
    yield connection
    connection.close()


@pytest.fixture
def synth_db(synth: Path, tmp_path: Path) -> Path:
    db_path = tmp_path / "synth.db"
    build_database(synth, db_path)
    return db_path


@pytest.fixture
def synth_conn(synth_db: Path):
    connection = connect(synth_db)
    yield connection
    connection.close()


# ---------------------------------------------------------------- 辅助函数


def replace_in(path: Path, old: str, new: str) -> Path:
    """在文件里做字符串替换（测试用改坏数据）。"""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"待替换文本不存在：{old!r} @ {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def error_text(result) -> str:
    return "\n".join(issue.format() for issue in result.errors)


def warning_text(result) -> str:
    return "\n".join(issue.format() for issue in result.warnings)
