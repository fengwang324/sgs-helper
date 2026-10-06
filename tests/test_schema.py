"""数据模型（Pydantic）测试：写错的字段必须报错，而不是被静默忽略。

精简后的模型只保留实际会展示 / 查询 / 核验的字段：

- 实体卡类（Product / GeneralVariant / CardPrinting）**没有** ``source``；
- ``Source`` 只剩 ``type`` + ``reference``；
- 通用 ``notes`` 一律由 ``review_note`` 取代；
- 技能版本不再按日期推导，武将版本用 ``skills[].version_id`` 显式绑定。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sgs_helper.schema import (
    Card,
    CardPrinting,
    Faq,
    General,
    GeneralSkillRef,
    GeneralVariant,
    Keyword,
    Product,
    Skill,
    SkillVersion,
    Source,
    Timing,
)

SOURCE = {"type": "community_db", "reference": ""}


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
    assert version.verification_status == "unverified"


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
    """Product / GeneralVariant / CardPrinting 的来源由 product_id 定位，不再重复保存。"""
    with pytest.raises(ValidationError):
        Product(id="product_x", name="X", source=SOURCE)
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_x", general_id="general_x", source=SOURCE)
    with pytest.raises(ValidationError):
        CardPrinting(
            id="card_x_a",
            card_id="card_x",
            name_printed="X",
            product_id="product_x",
            suit="spade",
            source=SOURCE,
        )


def test_source_is_required_where_rules_demand_it() -> None:
    with pytest.raises(ValidationError):
        SkillVersion(id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="文本")
    with pytest.raises(ValidationError):
        Faq(id="faq_x", question="问题", answer="回答")
    with pytest.raises(ValidationError):
        Keyword(id="keyword_x", name="关键词", definition="定义")
    with pytest.raises(ValidationError):
        Timing(id="timing_x", name="时点")


# ---------------------------------------------------------------- 卡牌


def test_rank_letters_are_normalized() -> None:
    base = dict(
        id="card_x_a",
        card_id="card_x",
        name_printed="X",
        product_id="product_x",
        suit="spade",
    )
    assert CardPrinting(**base, rank="A").rank == 1
    assert CardPrinting(**base, rank="J").rank == 11
    assert CardPrinting(**base, rank="Q").rank == 12
    assert CardPrinting(**base, rank="K").rank == 13
    assert CardPrinting(**base, rank="7").rank == 7
    assert CardPrinting(**base, rank=7).rank == 7


def test_rank_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CardPrinting(
            id="card_x_a",
            card_id="card_x",
            name_printed="X",
            product_id="product_x",
            rank=14,
        )


def test_card_printing_mode_is_not_stored() -> None:
    """CardPrinting.mode 由 product_id -> Product.mode 推导，不再是模型字段。"""
    assert "mode" not in CardPrinting.model_fields
    with pytest.raises(ValidationError):
        CardPrinting(
            id="card_x_a",
            card_id="card_x",
            name_printed="X",
            product_id="product_x",
            mode="guozhan",
        )


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
    assert isinstance(variant.skills[0], GeneralSkillRef)
    assert variant.skills[0].version_id is None
    assert variant.skills[1].version_id == "skill_y_v1"


# ---------------------------------------------------------------- review_note / 已删字段


def test_notes_is_replaced_by_review_note() -> None:
    assert set(Skill.model_fields) == {"id", "name", "review_note"}
    skill = Skill(id="skill_x", name="X", review_note="待复核")
    assert skill.review_note == "待复核"
    with pytest.raises(ValidationError):
        Skill(id="skill_x", name="X", notes="旧字段")

    card = Card(id="card_x", name="X", card_type="basic", review_note="待复核")
    assert card.review_note == "待复核"
    with pytest.raises(ValidationError):
        Card(id="card_x", name="X", card_type="basic", notes="旧字段")

    with pytest.raises(ValidationError):
        CardPrinting(
            id="card_x_a",
            card_id="card_x",
            name_printed="X",
            product_id="product_x",
            notes="旧字段",
        )


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


def test_verification_status_only_on_allowed_kinds() -> None:
    assert "verification_status" not in Product.model_fields
    assert "verification_status" not in General.model_fields
    assert "verification_status" not in Skill.model_fields
    assert "verification_status" not in Card.model_fields
    for model in (GeneralVariant, SkillVersion, CardPrinting, Keyword, Faq, Timing):
        assert "verification_status" in model.model_fields
