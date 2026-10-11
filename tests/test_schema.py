"""数据模型（Pydantic）测试：写错的字段必须报错，而不是被静默忽略。

精简后的模型只保留实际会展示 / 查询 / 核验的字段：

- ``SkillVersion`` 只保留 id / skill_id / mode / product_id / text；
- FAQ 的 ``source`` 保留原始多行文本，来源实体已删除；
- 通用 ``notes`` 与 ``review_note`` 都已整体删除；
- 技能版本不再按日期推导，武将版本用 ``skills[].version_id`` 显式绑定；
- ``Card`` 是**单层**的：``category_1/2/3`` 决定它是什么，``card_set`` 决定它来自哪个
  拓展包；同一张卡不再因为花色 / 点数不同拆成多条 ``CardPrinting``；
- 技能引用 ``SkillRef`` 只用于武将版本，卡牌效果保持字符串。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sgs_helper.constants import CARD_CATEGORY_TREE
from sgs_helper.schema import (
    Card,
    Faq,
    General,
    GeneralVariant,
    Keyword,
    Product,
    Skill,
    SkillRef,
    SkillVersion,
)

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
    "effect_text": "旧效果列",
    "effect_skills": [],
    "illustrator": "画师",
    "bottom_right_mark": "君",
    "card_number": "001",
    "related_keywords": ["keyword_x"],
    "distance_modifier": "+1",
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

    version = SkillVersion(id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="文本")
    assert version.mode == "guozhan"
    assert set(SkillVersion.model_fields) == {"id", "skill_id", "mode", "product_id", "text"}


def test_strings_are_stripped() -> None:
    product = Product(id=" product_x ", name=" X ", mode="guozhan")
    assert product.id == "product_x"
    assert product.name == "X"


# ---------------------------------------------------------------- 来源


def test_faq_source_is_text_and_skill_source_is_removed() -> None:
    # FAQ source 仍保留原始文本。
    with pytest.raises(ValidationError):
        Faq(id="faq_x", question="问题", answer="回答")
    faq = Faq(id="faq_x", question="问题", answer="回答", source="来源原文")
    assert faq.source == "来源原文"
    with pytest.raises(ValidationError):
        SkillVersion(id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="文本", source={"type": "wiki"})
    # 关键词只有 id / name / definition / variant 四个字段，没有来源字段
    assert Keyword(id="keyword_x", name="关键词", definition="定义").variant == ""
    with pytest.raises(ValidationError):
        Keyword(id="keyword_x", name="关键词", definition="定义", source="文本")


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
        "attack_range",
        "deck_info",
        "note",
        "recast",
        "rules",
    }
    card = _card()
    assert card.image == ""
    assert card.card_set is None
    assert card.effect == ""
    assert card.attack_range == ""
    assert card.deck_info == ""
    assert card.note == ""


def test_basic_card_category_three_is_the_family() -> None:
    """基本牌最后一级习惯写牌族（杀 / 闪 / 桃 / 酒），具体哪一张写在 name 里。

    这只是本项目的录入**习惯**（也是录入页的候选来源），不是校验规则：
    分类可以自由填写，校验器不因为填了「火杀」而报警告。
    """
    families = CARD_CATEGORY_TREE["游戏牌"]["基本牌"]
    assert families == ["杀", "闪", "桃", "酒"]

    huo_sha = _card(category_2="基本牌", category_3="杀", name="火杀")
    assert huo_sha.category_3 == "杀" and huo_sha.name == "火杀"

    lve_shan = _card(category_2="基本牌", category_3="闪", name="掠闪")
    assert lve_shan.category_3 == "闪" and lve_shan.name == "掠闪"


def test_card_categories_are_free_text_and_may_be_blank() -> None:
    """分类自由填写：没有预设的取值也不报错；二 / 三级可以留空。

    「指示物」在 ``CARD_CATEGORY_TREE`` 里刻意不预置任何子分类（只有一级入口）。
    """
    assert CARD_CATEGORY_TREE["指示物"] == {}

    free = _card(category_1="指示物", category_2="鏖战", category_3="鏖战说明卡", name="鏖战效果面")
    assert (free.category_2, free.category_3) == ("鏖战", "鏖战说明卡")

    only_level_1 = _card(category_1="指示物", category_2="", category_3="", name="只有一级")
    assert only_level_1.category_2 == "" and only_level_1.category_3 == ""


def test_card_effect_and_attack_range_are_text() -> None:
    card = _card(effect="锁定技，造成 X 点伤害。", attack_range="X+1")
    assert card.effect == "锁定技，造成 X 点伤害。"
    assert card.attack_range == "X+1"
    with pytest.raises(ValidationError):
        _card(effect={"text": "旧嵌套结构", "skills": []})


def test_card_recast_is_a_default_false_game_card_flag() -> None:
    assert _card().recast is False
    assert _card(recast=True).recast is True
    with pytest.raises(ValidationError):
        _card(recast="not a boolean")


def test_card_rules_are_use_methods_without_note() -> None:
    card = _card(rules={"use_methods": [{"label": "使用", "timing": "出牌阶段", "target": "一名角色", "effect": "造成伤害"}]})
    assert card.rules.use_methods[0].label == "使用"
    with pytest.raises(ValidationError):
        _card(rules={"use_methods": [{"note": "旧字段"}]})
    with pytest.raises(ValidationError):
        _card(rules={"distance_modifier": "+1"})


def test_indicator_rejects_game_card_fields() -> None:
    for field, value in (("rules", {"use_methods": []}), ("card_set", "牌堆"), ("deck_info", "1张"), ("attack_range", "5"), ("recast", True)):
        with pytest.raises(ValidationError):
            _card(category_1="指示物", **{field: value})


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
        )
    assert "effective_from" in str(excinfo.value)


def test_empty_rule_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillVersion(
            id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="   "
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
        general_number="G.SH 001",
        mode="identity",
        faction="蜀",
        hp=3,
        title="迟暮的丞相",
    )
    guozhan = GeneralVariant(
        id="variant_zhu_ge_liang_guo_zhan_ban",
        general_id=general.id,
        general_number="G.SH 002",
        mode="guozhan",
        faction="蜀",
        hp=1.5,
    )
    assert standard.general_id == guozhan.general_id == general.id
    assert standard.general_number != guozhan.general_number
    assert "variant" not in GeneralVariant.model_fields
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_old", general_id=general.id, variant="标准版")


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


# ---------------------------------------------------------------- 已删字段


def test_review_note_is_gone_from_every_model() -> None:
    """review_note 已整体删除：模型里没有它，写了必须报错。"""
    assert set(Skill.model_fields) == {"id", "name"}
    with pytest.raises(ValidationError):
        Skill(id="skill_x", name="X", review_note="待复核")
    with pytest.raises(ValidationError):
        Skill(id="skill_x", name="X", notes="旧字段")

    with pytest.raises(ValidationError):
        _card(review_note="待复核")
    with pytest.raises(ValidationError):
        _card(notes="旧字段")


def test_removed_relation_fields_are_rejected() -> None:
    """related_* / parent_id / previous_ids / next_ids 全部删除，写了必须报错。"""
    with pytest.raises(ValidationError):
        Faq(
            id="faq_x", question="问题", answer="回答", source="- type: community_db",
            related_skill_ids=["skill_x"],
        )
    with pytest.raises(ValidationError):
        Keyword(
            id="keyword_x", name="关键词", definition="定义",
            related_keyword_ids=["keyword_y"], notes="旧字段",
        )
    with pytest.raises(ValidationError):
        Keyword(
            id="keyword_x", name="关键词", definition="定义",
            related_timing_ids=["timing_x"],
        )


def test_timing_kind_is_gone_from_every_model() -> None:
    """「时点」整体舍弃：模型里不再有 Timing，关键词也不再有 related_timing_ids。"""
    import sgs_helper.schema as schema

    assert not hasattr(schema, "Timing")
    assert "timing" not in schema.MODEL_BY_KIND
    assert set(Keyword.model_fields) == {"id", "name", "definition", "variant"}


def test_verification_status_is_gone_from_every_model() -> None:
    """核验状态已从数据模型整体删除，任何实体都不该再有这个字段。"""
    for model in (Product, General, GeneralVariant, Skill, SkillVersion,
                  Card, Keyword, Faq):
        assert "verification_status" not in model.model_fields, model.__name__
