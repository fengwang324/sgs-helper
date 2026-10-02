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
#: 实体卡上的编号（**不是**内部 id）：允许大小写字母、数字、``-``、``_``
NUMBER_PATTERN = r"^[A-Za-z0-9_-]+$"


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
    """武将 → 技能的关系（以及这个武将**具体用哪一版**技能文本）。

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
    """武将。只引用技能（以及可选的具体版本），不把技能规则文本写死。

    **只有 ``name`` 是必须填的**：这个库要允许“先录手头已有的信息、以后再补全”，
    不能因为缺一个字段就整条保存不了。
    """

    id: Id
    name: Name
    #: 称号（武将卡上的称号，例如“独眼的罗刹”）
    title: str = ""
    #: 实体卡上的编号（例如 WEI001 / shu-01）。**不是**内部 id，允许大小写字母/数字/-/_。
    general_number: str | None = None
    mode: Mode | None = None
    faction: str = ""
    #: 勾玉（体力）。允许为空：先录入名字、以后再补体力。
    hp: float | None = Field(default=None, gt=0, le=10)
    gender: Gender = Gender.unknown
    product_id: Id | None = None
    #: 武将牌系列 / 武将牌组（替代旧的、含义不清的 ``package``）
    general_set: str | None = None
    #: 技能关系（含可选的具体版本绑定）；顺序即卡面上的技能顺序
    skills: list[GeneralSkillRef] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    source: Source | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified
    notes: str = ""

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
                "编号只能包含字母、数字、- 和 _（例如 WEI001、shu-01）；"
                "内部 id 是另一回事，会自动生成"
            )
        return text


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
