"""数据模型（Pydantic）测试：写错的字段必须报错，而不是被静默忽略。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sgs_helper.schema import (
    CardPrinting,
    General,
    GeneralVariant,
    Product,
    SkillVersion,
    Source,
)

SOURCE = {"type": "community_db", "title": "测试来源"}


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
    assert product.verification_status == "unverified"


def test_rank_letters_are_normalized() -> None:
    base = dict(
        id="card_x_a",
        card_id="card_x",
        name_printed="X",
        mode="guozhan",
        product_id="product_x",
        suit="spade",
        source=SOURCE,
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
            mode="guozhan",
            product_id="product_x",
            rank=14,
            source=SOURCE,
        )


def test_effective_range_reversed_is_rejected() -> None:
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
    assert "effective_to" in str(excinfo.value)


def test_release_year_must_match_release_date() -> None:
    with pytest.raises(ValidationError):
        Product(
            id="product_x",
            name="X",
            mode="guozhan",
            release_year=2024,
            release_date="2023-01-01",
        )
    Product(id="product_x", name="X", mode="guozhan", release_year=2024, release_date="2024-01-01")


def test_empty_rule_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillVersion(
            id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="   ", source=SOURCE
        )


def test_hp_bounds() -> None:
    """体力属于 GeneralVariant（武将个体不存体力）。"""
    GeneralVariant(id="variant_x", general_id="general_x", mode="guozhan", hp=1.5)
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_x", general_id="general_x", hp=0)
    with pytest.raises(ValidationError):
        GeneralVariant(id="variant_x", general_id="general_x", hp=99)


def test_general_stays_stable_and_variant_carries_the_rest() -> None:
    """诸葛亮 / 界诸葛亮 是两个 General；标准版与国战版是同一个 General 的两个 Variant。"""
    assert set(General.model_fields) == {"id", "name", "aliases", "notes"}
    general = General(id="general_zhu_ge_liang", name="诸葛亮", aliases=["老诸葛"])
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


def test_source_requires_known_type() -> None:
    with pytest.raises(ValidationError):
        Source(type="random_blog")
    assert Source(type="wiki").type == "wiki"


def test_source_is_required_where_rules_demand_it() -> None:
    with pytest.raises(ValidationError):
        SkillVersion(id="skill_x_v1", skill_id="skill_x", mode="guozhan", text="文本")


def test_strings_are_stripped() -> None:
    product = Product(id=" product_x ", name=" X ", mode="guozhan")
    assert product.id == "product_x"
    assert product.name == "X"
