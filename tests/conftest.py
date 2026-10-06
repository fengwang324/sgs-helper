"""pytest 共享夹具。

两个数据来源：

1. ``data_dir`` / ``built_db`` / ``conn`` —— 使用仓库里真实的 ``data/``；
2. ``synth`` / ``synth_db`` / ``synth_conn`` —— 测试自带的**合成数据集**，
   用固定 id 验证引用完整性、技能版本绑定、按名称自动识别等行为，
   不依赖真实数据的增删。
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

#: 精简后的来源：只有 type + reference（展示名由渲染层推导）
SOURCE_YAML = """source:
  type: community_db
  reference: ""
"""

# ---------------------------------------------------------------- 合成数据集
# 设计要点：
# - variant_demo_alpha_guozhan 的技能**不写 version_id**，靠 mode 回退匹配国战版本；
# - variant_demo_alpha_identity 显式绑定 skill_demo_identity_v1；
# - variant_demo_beta_identity 同样不写 version_id，但身份局有两代版本 → 绑定不上；
# - FAQ / 技能文本里的关联实体靠名称自动识别，不再有 related_* 字段。

SYNTHETIC: dict[str, str] = {
    "products/demo_a.yaml": """id: product_demo_a
name: 演示产品A
mode: guozhan
release_year: 2024
general_sets:
  - 演示武将牌系列
card_sets:
  - 演示牌堆
""",
    "products/demo_b.yaml": """id: product_demo_b
name: 演示产品B
mode: identity
release_year: 2020
general_sets:
  - 演示武将牌系列
card_sets:
  - 演示牌堆
""",
    # 武将个体：只有 id + name
    "generals/demo_alpha.yaml": """id: general_demo_alpha
name: 演示武将甲
""",
    "generals/demo_beta.yaml": """id: general_demo_beta
name: 演示武将乙
""",
    "general_variants/demo_alpha_guozhan.yaml": """id: variant_demo_alpha_guozhan
general_id: general_demo_alpha
variant: 国战版
title: 演示称号甲
general_number: G.WEI 001
illustrator: 演示画师
image: variant_demo_alpha_guozhan.jpg
mode: guozhan
faction: 群
hp: 1.5
gender: male
product_id: product_demo_a
general_set: 演示武将牌系列
zhulianbihe:
  - 演示武将乙
skills:
  - skill_id: skill_demo
review_note: 演示用待复核说明
""",
    "general_variants/demo_alpha_identity.yaml": """id: variant_demo_alpha_identity
general_id: general_demo_alpha
variant: 标准版
mode: identity
faction: 群
hp: 3
gender: male
skills:
  - skill_id: skill_demo
    version_id: skill_demo_identity_v1
""",
    "general_variants/demo_beta_identity.yaml": """id: variant_demo_beta_identity
general_id: general_demo_beta
variant: 标准版
mode: identity
faction: 蜀
hp: 3
gender: female
product_id: product_demo_b
skills:
  - skill_id: skill_demo
""",
    "skills/demo.yaml": """id: skill_demo
name: 演示技能
""",
    "skills/demo_other.yaml": """id: skill_demo_other
name: 另一个演示技能
review_note: 演示用技能名待复核
""",
    "skill_versions/demo_other_v1.yaml": f"""id: skill_demo_other_v1
skill_id: skill_demo_other
mode: guozhan
text: 另一个技能的演示文本
version_label: 演示初版
product_id: product_demo_a
{SOURCE_YAML}""",
    "skill_versions/demo_guozhan_v1.yaml": f"""id: skill_demo_guozhan_v1
skill_id: skill_demo
mode: guozhan
text: 国战版演示技能文本
version_label: 国战初版
product_id: product_demo_a
previous_version_id: null
{SOURCE_YAML}""",
    "skill_versions/demo_identity_v1.yaml": f"""id: skill_demo_identity_v1
skill_id: skill_demo
mode: identity
text: 身份局一代演示技能文本
version_label: 身份局初版
product_id: product_demo_b
{SOURCE_YAML}""",
    "skill_versions/demo_identity_v2.yaml": f"""id: skill_demo_identity_v2
skill_id: skill_demo
mode: identity
text: 身份局二代演示技能文本
version_label: 平衡调整后
product_id: null
previous_version_id: skill_demo_identity_v1
{SOURCE_YAML}review_note: 演示用待复核说明
""",
    "cards/demo_card.yaml": """id: card_demo
category_1: 游戏牌
category_2: 基本牌
category_3: 杀
name: 演示卡牌
image: ""
card_set: null
effect:
  text: ""
  skills: []
review_note: 演示用待复核说明
""",
    "cards/demo_trick.yaml": """id: card_demo_trick
category_1: 游戏牌
category_2: 锦囊牌
category_3: 非延时锦囊
name: 演示锦囊
image: ""
card_set: 演示牌堆
effect:
  text: 演示效果：与演示技能相关。
  skills:
    - skill_id: skill_demo
      version_id: skill_demo_guozhan_v1
review_note: ""
""",
    "faq/demo_1.yaml": f"""id: faq_demo_1
question: 演示问题：演示技能的结算？
answer: 演示答案：演示技能与演示卡牌一起结算。
mode: identity
{SOURCE_YAML}""",
    "keywords/demo.yaml": f"""id: keyword_demo
name: 演示关键词
definition: 演示用规则关键词定义
mode: common
{SOURCE_YAML}""",
    "timings/demo.yaml": f"""id: timing_demo
name: 演示时点
description: 演示用时点说明
mode: common
{SOURCE_YAML}""",
    "timings/demo_next.yaml": f"""id: timing_demo_next
name: 演示后续时点
description: 演示用的后续时点
mode: common
{SOURCE_YAML}""",
    "keywords/demo2.yaml": f"""id: keyword_demo2
name: 演示关键词二
definition: 第二个演示关键词
mode: common
{SOURCE_YAML}""",
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
    """合成数据集（固定 id）。"""
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
