"""Pydantic 数据模型：与 YAML 字段一一对应。

约定：

- 所有模型 ``extra="forbid"``：YAML 里写错字段名会直接报错，绝不静默丢弃，
  这样“看起来写对了但没生效”的错误不可能发生。
- 枚举字段在模型里保留为字符串（``use_enum_values=True``），落库与返回 JSON 都很直接。
- 多值字段统一用 ``list``，禁止用空字符串冒充空值（空字符串会撞 id 正则而报错）。
- ``source`` 内嵌结构在需要追溯来源的实体上是**必填**的。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

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
#: 实体卡上的编号（**不是**内部 id）：允许大小写字母、数字、``-``/``_``/``.``/``/`` 和空格，
#: 以便与卡面完全一致（国战编号形如 ``G.WEI 075``、``G.WEI/SHU 079``）
NUMBER_PATTERN = r"^[A-Za-z0-9_\-./& ]+$"


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
    """真实发行的某个线下产品（国战产品 / 扩展盒 / 身份局产品 / 补充包…）。

    日常录入只关心：名称 / 发行年份 / 模式 / 武将牌系列 / 游戏牌牌堆。
    其余字段（出版方、版次、来源…）仍然保留，但**允许为空**，不阻塞录入。

    ``general_sets`` 与 ``card_sets`` 是两件不同的事，必须分开：

    - ``general_sets``：这个产品里的**武将牌系列 / 武将牌组名称**（国战标准、势备篇…）；
    - ``card_sets``：这个产品里的**游戏牌牌堆 / 牌堆系列名称**（标准牌堆、九变牌堆…）。

    旧字段 ``included_sets`` 把两者混在一起，已废弃。
    """

    id: Id
    name: Name
    series: str = ""
    mode: Mode | None = None
    publisher: str = ""
    release_year: int | None = Field(default=None, ge=1990, le=2100)
    release_date: date | None = None
    edition: str = ""
    #: 武将牌系列 / 武将牌组名称（可以有多个）
    general_sets: list[str] = Field(default_factory=list)
    #: 游戏牌牌堆 / 牌堆系列名称（可以有多个）
    card_sets: list[str] = Field(default_factory=list)
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


class GeneralSkillRef(StrictModel):
    """武将版本 → 技能的关系（以及这个版本**具体用哪一版**技能文本）。

    为什么不用 ``skill_ids: [...]`` 两个平行数组：平行数组一旦顺序错位就会静默张冠李戴，
    而且表达不了“实体武将卡上印的到底是哪一版”。

    - ``skill_id``：必填，指向 SkillDefinition；
    - ``version_id``：可以为空。为空表示“还没确认具体版本”，详情页按
      mode + effective_from/effective_to 自动推导当前有效版本作为 fallback；
      不为空时必须属于同一个 ``skill_id``（校验器强制）。
    """

    skill_id: Id
    version_id: Id | None = None


class General(StrictModel):
    """**武将个体**。只保存相对稳定的信息。

    ``诸葛亮`` / ``界诸葛亮`` / ``神诸葛亮`` 是**不同**的 General（官方武将名不同）；
    而“标准版诸葛亮”与“国战版诸葛亮”官方名都还是``诸葛亮``，
    它们是同一个 General 的两个 :class:`GeneralVariant`。

    模式、势力、体力、性别、称号、产品、武将牌系列、技能配置**都不在这里**，
    一律放在 GeneralVariant 上 —— 那些本来就是“版本之间会变”的东西。
    """

    id: Id
    name: Name
    notes: str = ""


class GeneralVariant(StrictModel):
    """某个玩法 / 版本下的武将数据（身份局标准版、国战版、界限突破…）。

    只有 ``name`` 之类的稳定信息留在 General，其余全部在这里：

    - ``variant``：版本标签，例如 ``标准版`` / ``国战版`` / ``界限突破``；
    - ``mode``：游戏模式；身份局标准版记正常体力（3/4/5），
      国战按规则意义上的阴阳鱼记 1.5 / 2 / 2.5（**不要**把半阴阳鱼记成 3/4/5）；
    - 其余字段可以留空，先录手头有的信息。
    """

    id: Id
    general_id: Id
    #: 版本标签（同一武将的多个版本靠它区分，例如 标准版 / 国战版）
    variant: str = "标准版"
    title: str = ""
    general_number: str | None = None
    illustrator: str = ""
    image: str = ""
    mode: Mode | None = None
    faction: str = ""
    hp: float | None = Field(default=None, gt=0, le=10)
    gender: Gender = Gender.unknown
    product_id: Id | None = None
    general_set: str | None = None
    #: 珠联璧合：可写多个武将名，也可以是类别值（如 男性武将 / 群势力武将 / 君主）
    zhulianbihe: list[str] = Field(default_factory=list)
    #: 技能关系（含可选的具体版本绑定）；顺序即卡面上的技能顺序
    skills: list[GeneralSkillRef] = Field(default_factory=list)
    source: Source | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""

    @field_serializer("hp")
    def _serialize_hp(self, value: float | None) -> float | int | None:
        """体力写成 3 / 4 / 5 而不是 3.0；国战半勾玉仍然是 1.5。"""
        if value is None:
            return None
        number = float(value)
        return int(number) if number.is_integer() else number

    @field_validator("general_number", mode="before")
    @classmethod
    def _normalize_number(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if not re.fullmatch(NUMBER_PATTERN, text):
            raise ValueError(
                "编号只能包含字母、数字、- _ . / & 和空格（例如 WEI001、G.WEI 075、"
                "G.WEI/SHU 079）；内部 id 是另一回事，会自动生成"
            )
        return text


# ---------------------------------------------------------------- 技能


class Skill(StrictModel):
    """SkillDefinition：技能**本体**（例如“集智”这个概念）。

    技能名不是主键；同名技能的不同模式/产品/时期文本全部放在 SkillVersion。
    """

    id: Id
    name: Name
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
    #: 卡面插画师（卡面左下 / 编号左边那行小字）
    illustrator: str = ""
    #: 卡图相对路径（相对 data/ 目录）
    image: str = ""
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


class GeneralSet(StrictModel):
    """**武将牌系列 / 包**，支持层级。

    例：``君临天下`` 是父系列，``君临天下·变`` 是它的子系列；
    武将版本只关联**最具体**的那个（``general_set: 君临天下·变``），
    上层系列靠 ``parent_id`` 推出来，不用在武将上重复写。
    """

    id: Id
    name: Name
    #: 上级系列（顶层留空）
    parent_id: Id | None = None
    product_id: Id | None = None
    #: 同级排序用（数字小的排前面）
    sort_order: int = 0
    notes: str = ""
    source: Source | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified


#: kind -> 模型类
MODEL_BY_KIND: dict[str, type[StrictModel]] = {
    "product": Product,
    "general_set": GeneralSet,
    "general": General,
    "general_variant": GeneralVariant,
    "skill": Skill,
    "skill_version": SkillVersion,
    "card": Card,
    "card_printing": CardPrinting,
    "faq": Faq,
    "keyword": Keyword,
    "timing": Timing,
}
