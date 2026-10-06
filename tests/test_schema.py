"""数据模型（Pydantic）测试：写错的字段必须报错，而不是被静默忽略。

精简后的模型只保留实际会展示 / 查询 / 核验的字段：

- 实体卡类（Product / GeneralVariant / Card）**没有** ``source``；
- ``Source`` 只剩 ``type`` + ``reference``；
- 通用 ``notes`` 一律由 ``review_note`` 取代；
- 技能版本不再按日期推导，武将版本用 ``skills[].version_id`` 显式绑定；
- ``Card`` 是**单层**的：``category_1/2/3`` 决定它是什么，``card_set`` 决定它来自哪个
  拓展包；同一张卡不再因为花色 / 点数不同拆成多条 ``CardPrinting``；
- 技能引用统一为 ``SkillRef``（武将版本与卡牌效果共用）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sgs_helper.constants import CARD_CATEGORY_TREE
from sgs_helper.schema import (
    Card,
    CardEffect,
    Faq,
    General,
    GeneralVariant,
    Keyword,
    Product,
    Skill,
    SkillRef,
    SkillVersion,
    Source,
    Timing,
)

SOURCE = {"type": "community_db", "reference": ""}

#: 一张合法基本牌的最小字段集（基本牌的 category_3 是牌族，具体哪张写在 name）
CARD_BASE = {
    "category_1": "游戏牌",
    "category_2": "基本牌",
    "category_3": "杀",
    "name": "杀",
}

#: 卡牌从两层（Card + CardPrinting）收敛成一层后删掉的字段：写了就要报未知字段
REMOVED_CARD_FIELDS = {
    "card_type": "basic",
    "subtype": "normal",
    "name_printed": "杀",
    "card_id": "card_x",
    "product_id": "product_x",
    "suit": "spade",
    "rank": 1,
    "quantity": 2,
    "printed_text": "卡面原文",
    "normalized_text": "规范化正文",
    "illustrator": "画师",
    "bottom_right_mark": "君",
    "card_number": "001",
    "related_keywords": ["keyword_x"],
}


def _card(**overrides) -> Card:
    return Card(**{"id": "card_x", **CARD_BASE, **overrides})


# ---------------------------------------------------------------- 基础约束


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Product(id="product_x", name="X", mode="guozhan", version="v1")
    assert "version" in str(excinfo.value)
    assert "unknown" in str(excinfo.value).lower() or "extra" in str(excinfo.value).lower()


def test_invalid_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Product(id="product_x", name="X", mode="不存在的模式")


def test_invalid_id_pattern_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Product(id="Product-X", name="X", mode="guozhan")


def test_enum_values_stay_strings() -> None:
    product = Product(id="product_x", name="X", mode="guozhan")
    assert product.mode == "guozhan"
    assert product.general_sets == [] and product.card_sets == []

    version = SkillVersion(
        id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="文本", source=SOURCE
    )
    assert version.mode == "guozhan"


def test_strings_are_stripped() -> None:
    product = Product(id=" product_x ", name=" X ", mode="guozhan")
    assert product.id == "product_x"
    assert product.name == "X"


# ---------------------------------------------------------------- 来源


def test_source_is_type_plus_reference_only() -> None:
    """Source 只剩 type + reference：旧的 title / notes 结构必须拒绝。"""
    assert Source(type="wiki").reference == ""
    assert Source(type="wiki", reference="https://example.com").reference == "https://example.com"
    with pytest.raises(ValidationError):
        Source(type="wiki", title="测试来源")
    with pytest.raises(ValidationError):
        Source(type="wiki", notes="旧字段")


def test_source_requires_known_type() -> None:
    with pytest.raises(ValidationError):
        Source(type="random_blog")
    assert Source(type="wiki").type == "wiki"


def test_entity_card_kinds_have_no_source() -> None:
    """Product / GeneralVariant / Card 的来源由 product_id 或数据目录定位，不再重复保存。"""
    with pytest.raises(ValidationError):
        Product(id="product_x", name="X", source=SOURCE)
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_x", general_id="general_x", source=SOURCE)
    with pytest.raises(ValidationError):
        _card(source=SOURCE)


def test_source_is_required_where_rules_demand_it() -> None:
    with pytest.raises(ValidationError):
        SkillVersion(id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="文本")
    with pytest.raises(ValidationError):
        Faq(id="faq_x", question="问题", answer="回答")
    with pytest.raises(ValidationError):
        Keyword(id="keyword_x", name="关键词", definition="定义")
    with pytest.raises(ValidationError):
        Timing(id="timing_x", name="时点")


# ---------------------------------------------------------------- 卡牌（单层）


def test_card_is_single_layer() -> None:
    """Card 只有一层：分类 / 卡名 / 卡图 / 拓展包 / 效果，没有 CardPrinting。"""
    assert set(Card.model_fields) == {
        "id",
        "category_1",
        "category_2",
        "category_3",
        "name",
        "image",
        "card_set",
        "effect",
        "review_note",
    }
    card = _card()
    assert card.image == ""
    assert card.card_set is None
    assert card.effect == CardEffect()
    assert card.review_note == ""


def test_basic_card_category_three_is_the_family() -> None:
    """基本牌最后一级是牌族（杀 / 闪 / 桃 / 酒），具体是哪一张写在 name 里。"""
    families = CARD_CATEGORY_TREE["游戏牌"]["基本牌"]
    assert families == ["杀", "闪", "桃", "酒"]

    huo_sha = _card(category_2="基本牌", category_3="杀", name="火杀")
    assert huo_sha.category_3 == "杀" and huo_sha.name == "火杀"

    lve_shan = _card(category_2="基本牌", category_3="闪", name="掠闪")
    assert lve_shan.category_3 == "闪" and lve_shan.name == "掠闪"


def test_card_effect_allows_empty_text_and_skills() -> None:
    """effect.text 与 effect.skills 都允许为空，也可以同时为空。"""
    card = _card(effect={"text": "", "skills": []})
    assert isinstance(card.effect, CardEffect)
    assert card.effect.text == "" and card.effect.skills == []


def test_card_effect_skills_use_skill_ref() -> None:
    """卡牌效果里的技能引用与武将版本共用 SkillRef（{skill_id, version_id}）。"""
    card = _card(
        category_2="装备牌",
        category_3="宝物",
        name="定澜夜明珠",
        card_set="九变篇",
        image="card_ding_lan_ye_ming_zhu_qlhd_diamond_6.jpg",
        effect={
            "text": "锁定技，你视为拥有“制衡”。",
            "skills": [
                {
                    "skill_id": "skill_zhi_heng",
                    "version_id": "skill_zhi_heng_guozhan_qlhd_v1",
                }
            ],
        },
    )
    ref = card.effect.skills[0]
    assert isinstance(ref, SkillRef)
    assert ref.skill_id == "skill_zhi_heng"
    assert ref.version_id == "skill_zhi_heng_guozhan_qlhd_v1"

    # 只写 skill_id 也合法：version_id 为空表示查询时按 mode 回退
    loose = _card(effect={"skills": [{"skill_id": "skill_zhi_heng"}]})
    assert loose.effect.skills[0].version_id is None


@pytest.mark.parametrize(("field", "value"), sorted(REMOVED_CARD_FIELDS.items()))
def test_removed_card_fields_are_rejected(field: str, value: object) -> None:
    """CardPrinting 及旧卡面字段被删除后，写进 Card 必须报未知字段。"""
    with pytest.raises(ValidationError) as excinfo:
        _card(**{field: value})
    assert field in str(excinfo.value)


def test_card_set_is_optional_but_must_be_a_string() -> None:
    assert _card(card_set=None).card_set is None
    assert _card(card_set="九变篇").card_set == "九变篇"


# ---------------------------------------------------------------- 技能 / 武将


def test_effective_range_is_gone() -> None:
    """技能版本不再有 package / effective_from / effective_to / change_note。"""
    with pytest.raises(ValidationError) as excinfo:
        SkillVersion(
            id="skill_x_v1",
            skill_id="skill_x",
            mode="guozhan",
            text="文本",
            effective_from="2025-01-01",
            effective_to="2024-01-01",
            source=SOURCE,
        )
    assert "effective_from" in str(excinfo.value)


def test_empty_rule_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillVersion(
            id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="   ", source=SOURCE
        )


def test_release_year_bounds() -> None:
    Product(id="product_x", name="X", mode="guozhan", release_year=1990)
    Product(id="product_x", name="X", mode="guozhan", release_year=2024)
    with pytest.raises(ValidationError):
        Product(id="product_x", name="X", mode="guozhan", release_year=1980)
    with pytest.raises(ValidationError) as excinfo:
        Product(id="product_x", name="X", mode="guozhan", release_date="2024-01-01")
    assert "release_date" in str(excinfo.value)


def test_hp_bounds() -> None:
    """体力属于 GeneralVariant（武将个体不存体力）。"""
    GeneralVariant(id="variant_x", general_id="general_x", mode="guozhan", hp=1.5)
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_x", general_id="general_x", hp=0)
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_x", general_id="general_x", hp=99)


def test_general_stays_stable_and_variant_carries_the_rest() -> None:
    """诸葛亮 / 界诸葛亮 是两个 General；标准版与国战版是同一个 General 的两个 Variant。"""
    assert set(General.model_fields) == {"id", "name"}
    general = General(id="general_zhu_ge_liang", name="诸葛亮")
    standard = GeneralVariant(
        id="variant_zhu_ge_liang_biao_zhun_ban",
        general_id=general.id,
        variant="标准版",
        mode="identity",
        faction="蜀",
        hp=3,
        title="迟暮的丞相",
    )
    guozhan = GeneralVariant(
        id="variant_zhu_ge_liang_guo_zhan_ban",
        general_id=general.id,
        variant="国战版",
        mode="guozhan",
        faction="蜀",
        hp=1.5,
    )
    assert standard.general_id == guozhan.general_id == general.id
    assert standard.variant != guozhan.variant


def test_general_sets_are_plain_strings() -> None:
    """GeneralSet 实体已删除：系列名只在 Product.general_sets 与 GeneralVariant.general_set。"""
    product = Product(
        id="product_x",
        name="X",
        mode="guozhan",
        general_sets=["国战标准"],
        card_sets=["标准牌堆"],
    )
    variant = GeneralVariant(
        id="variant_x",
        general_id="general_x",
        product_id=product.id,
        general_set="国战标准",
    )
    assert variant.general_set in product.general_sets
    assert "标准牌堆" in product.card_sets


def test_general_skill_ref_binds_version_explicitly() -> None:
    """version_id 是技能文本的唯一依据：可以留空（按 mode 回退），也可以显式绑定。"""
    variant = GeneralVariant(
        id="variant_x",
        general_id="general_x",
        mode="guozhan",
        skills=[
            {"skill_id": "skill_x"},
            {"skill_id": "skill_y", "version_id": "skill_y_v1"},
        ],
    )
    assert isinstance(variant.skills[0], SkillRef)
    assert variant.skills[0].version_id is None
    assert variant.skills[1].version_id == "skill_y_v1"


# ---------------------------------------------------------------- review_note / 已删字段


def test_notes_is_replaced_by_review_note() -> None:
    assert set(Skill.model_fields) == {"id", "name", "review_note"}
    skill = Skill(id="skill_x", name="X", review_note="待复核")
    assert skill.review_note == "待复核"
    with pytest.raises(ValidationError):
        Skill(id="skill_x", name="X", notes="旧字段")

    card = _card(review_note="待复核")
    assert card.review_note == "待复核"
    with pytest.raises(ValidationError):
        _card(notes="旧字段")


def test_removed_relation_fields_are_rejected() -> None:
    """related_* / parent_id / previous_ids / next_ids 全部删除，写了必须报错。"""
    with pytest.raises(ValidationError):
        Faq(
            id="faq_x", question="问题", answer="回答", source=SOURCE,
            related_skill_ids=["skill_x"],
        )
    with pytest.raises(ValidationError):
        Keyword(
            id="keyword_x", name="关键词", definition="定义", source=SOURCE,
            related_keyword_ids=["keyword_y"], notes="旧字段",
        )
    with pytest.raises(ValidationError):
        Keyword(
            id="keyword_x", name="关键词", definition="定义", source=SOURCE,
            related_timing_ids=["timing_x"],
        )
    with pytest.raises(ValidationError):
        Timing(id="timing_x", name="时点", source=SOURCE, parent_id="timing_y")
    with pytest.raises(ValidationError):
        Timing(id="timing_x", name="时点", source=SOURCE, previous_ids=["timing_y"])


def test_verification_status_is_gone_from_every_model() -> None:
    """核验状态已从数据模型整体删除，任何实体都不该再有这个字段。"""
    for model in (Product, General, GeneralVariant, Skill, SkillVersion,
                  Card, Keyword, Faq, Timing):
        assert "verification_status" not in model.model_fields, model.__name__
