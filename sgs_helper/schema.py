"""Pydantic 数据模型：与 YAML 字段一一对应。

约定：

- 所有模型 ``extra="forbid"``：YAML 里写错字段名会直接报错，绝不静默丢弃，
  这样“看起来写对了但没生效”的错误不可能发生。
- 枚举字段在模型里保留为字符串（``use_enum_values=True``），落库与返回 JSON 都很直接。
- 多值字段统一用 ``list``，禁止用空字符串冒充空值（空字符串会撞 id 正则而报错）。
- ``source`` 内嵌结构在需要追溯来源的实体上是**必填**的。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .constants import (
    ID_PATTERN,
    CardType,
    Gender,
    Mode,
    SourceType,
    Suit,
    VerificationStatus,
)

#: 实体 id：小写字母开头，只含小写字母/数字/下划线
Id = Annotated[str, Field(pattern=ID_PATTERN, min_length=2, max_length=120)]
#: 名称：不允许为空
Name = Annotated[str, Field(min_length=1, max_length=200)]
#: 规则文本：不允许为空
RuleText = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    """所有数据模型的基类。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class Source(StrictModel):
    """资料来源。第一版保持简单；需要时再扩展 url / local_file 等字段。"""

    type: SourceType
    title: str = ""
    reference: str = ""
    notes: str = ""


# ---------------------------------------------------------------- 实体产品


class Product(StrictModel):
    """真实发行的某个线下产品（国战产品 / 扩展盒 / 身份局产品 / 补充包…）。"""

    id: Id
    name: Name
    series: str = ""
    mode: Mode
    publisher: str = ""
    release_year: int | None = Field(default=None, ge=1990, le=2100)
    release_date: date | None = None
    edition: str = ""
    included_sets: list[str] = Field(default_factory=list)
    source: Source | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""

    @model_validator(mode="after")
    def _check_release(self):
        if (
            self.release_date is not None
            and self.release_year is not None
            and self.release_date.year != self.release_year
        ):
            raise ValueError(
                f"release_year({self.release_year}) 与 release_date({self.release_date}) 的年份不一致"
            )
        return self


# ---------------------------------------------------------------- 武将


class General(StrictModel):
    """武将。只引用技能 id，不把技能规则文本写死。"""

    id: Id
    name: Name
    mode: Mode
    faction: str = ""
    hp: float = Field(gt=0, le=10)
    gender: Gender = Gender.unknown
    product_id: Id | None = None
    package: str | None = None
    skill_ids: list[Id] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    source: Source | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""


# ---------------------------------------------------------------- 技能


class Skill(StrictModel):
    """SkillDefinition：技能**本体**（例如“集智”这个概念）。

    技能名不是主键；同名技能的不同模式/产品/时期文本全部放在 SkillVersion。
    """

    id: Id
    name: Name
    aliases: list[str] = Field(default_factory=list)
    notes: str = ""
    source: Source | None = None


class SkillVersion(StrictModel):
    """SkillVersion：某模式 / 某产品 / 某时期下技能的具体规则文本。

    更新技能的正确做法是**新增一条** SkillVersion 并用 ``previous_version_id``
    串链，绝不修改旧记录。
    """

    id: Id
    skill_id: Id
    mode: Mode
    text: RuleText
    version_label: str = ""
    product_id: Id | None = None
    package: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    previous_version_id: Id | None = None
    change_note: str = ""
    related_keywords: list[Id] = Field(default_factory=list)
    related_timing_ids: list[Id] = Field(default_factory=list)
    source: Source
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""

    @model_validator(mode="after")
    def _check_range(self):
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError(
                f"effective_to({self.effective_to}) 早于 effective_from({self.effective_from})"
            )
        return self


# ---------------------------------------------------------------- 卡牌


class Card(StrictModel):
    """CardDefinition：规则意义上的卡牌（杀 / 闪 / 桃 / 无懈可击 …）。"""

    id: Id
    name: Name
    card_type: CardType
    subtype: str = ""
    aliases: list[str] = Field(default_factory=list)
    related_keywords: list[Id] = Field(default_factory=list)
    notes: str = ""


class CardPrinting(StrictModel):
    """CardPrinting：某个实体产品中实际印刷出来的一张实体卡。

    ``product_id`` 必填 —— 必须能回答“这张实体卡来自哪个线下产品”。
    """

    id: Id
    card_id: Id
    name_printed: Name
    mode: Mode
    product_id: Id
    package: str | None = None
    card_set: str | None = None
    suit: Suit = Suit.none
    rank: int | None = Field(default=None, ge=1, le=13)
    quantity: int = Field(default=1, ge=1, le=99)
    printed_text: str = ""
    #: 额外检索用文本；**不能**覆盖 printed_text
    normalized_text: str = ""
    #: 右下角角标，单独保存，不写进 notes
    bottom_right_mark: str = ""
    card_number: str = ""
    source: Source
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""

    @field_validator("rank", mode="before")
    @classmethod
    def _normalize_rank(cls, value):
        """允许卡面写法 A/J/Q/K，统一归一化为 1..13。"""
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().upper()
            mapping = {"A": 1, "J": 11, "Q": 12, "K": 13}
            if text in mapping:
                return mapping[text]
            if text.isdigit():
                return int(text)
        return value


# ---------------------------------------------------------------- FAQ / 关键词 / 时点


class Faq(StrictModel):
    """规则问答 / 裁定。必须保留来源，不能只存一个脱离来源的答案。"""

    id: Id
    question: RuleText
    answer: RuleText
    mode: Mode = Mode.common
    related_general_ids: list[Id] = Field(default_factory=list)
    related_skill_ids: list[Id] = Field(default_factory=list)
    related_card_ids: list[Id] = Field(default_factory=list)
    related_keywords: list[Id] = Field(default_factory=list)
    related_timing_ids: list[Id] = Field(default_factory=list)
    source: Source
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""


class Keyword(StrictModel):
    """规则关键词，用于规则解释跳转。"""

    id: Id
    name: Name
    definition: RuleText
    aliases: list[str] = Field(default_factory=list)
    mode: Mode = Mode.common
    related_keyword_ids: list[Id] = Field(default_factory=list)
    related_timing_ids: list[Id] = Field(default_factory=list)
    source: Source
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""


class Timing(StrictModel):
    """规则时点，用于“时点速查”和后续的结算顺序。"""

    id: Id
    name: Name
    description: str = ""
    mode: Mode = Mode.common
    parent_id: Id | None = None
    previous_ids: list[Id] = Field(default_factory=list)
    next_ids: list[Id] = Field(default_factory=list)
    related_keywords: list[Id] = Field(default_factory=list)
    related_skill_ids: list[Id] = Field(default_factory=list)
    related_faq_ids: list[Id] = Field(default_factory=list)
    source: Source
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""


#: kind -> 模型类
MODEL_BY_KIND: dict[str, type[StrictModel]] = {
    "product": Product,
    "general": General,
    "skill": Skill,
    "skill_version": SkillVersion,
    "card": Card,
    "card_printing": CardPrinting,
    "faq": Faq,
    "keyword": Keyword,
    "timing": Timing,
}
