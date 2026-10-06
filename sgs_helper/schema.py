"""Pydantic 数据模型：与 YAML 字段一一对应。

设计目标（个人资料库 / 小程序查询 / 后续 AI 读取，不做通用数据库设计）：

- 只保留实际会**展示、查询、筛选、关联或核验**的字段；
- 能从其它实体推导的数据不重复保存（例如卡牌所属拓展包由 ``card_set`` 表达，不再挂产品）；
- 武将牌：``Product.general_sets`` + ``GeneralVariant.general_set`` 两处维护系列；
- 游戏牌 / 指示物：**一层** :class:`Card`（分类 + 卡名 + 卡图 + 拓展包 + 效果），
  同一规则卡不因为花色点数不同拆成多条；
- 规则类（SkillVersion / Keyword / FAQ / Timing）只保留极简来源 ``type`` + ``reference``；
- **没有通用 notes**：确实需要人工复核的地方用 ``review_note``。

约定：

- 所有模型 ``extra="forbid"``：YAML 里写错字段名会直接报错，绝不静默丢弃。
- 枚举字段在模型里保留为字符串（``use_enum_values=True``），落库与返回 JSON 都很直接。
- 多值字段统一用 ``list``，禁止用空字符串冒充空值。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from typing import Annotated

from .constants import (
    ID_PATTERN,
    Gender,
    Mode,
    SourceType,
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
    """规则资料来源：只保留 ``type`` + ``reference``。

    展示用的来源名（例如「三国杀官方规则集 3.0」）在渲染阶段由 type / reference 推导，
    不再单独存储。
    """

    type: SourceType
    reference: str = ""


# ---------------------------------------------------------------- 实体产品


class Product(StrictModel):
    """真实发行的某个线下产品（国战产品 / 扩展盒 / 身份局产品 / 补充包…）。

    ``general_sets`` 与 ``card_sets`` 是两件不同的事，必须分开：

    - ``general_sets``：这个产品里的**武将牌系列 / 武将牌组名称**（国战标准、势备篇…），
      它同时是 GeneralVariant.general_set 的唯一维护处；
    - ``card_sets``：这个产品里的**游戏牌拓展包名称**（九变篇、君主游戏牌…），
      它同时是 Card.card_set 的候选来源。注意「指示物」是卡牌分类，不是拓展包。
    """

    id: Id
    name: Name
    release_year: int | None = Field(default=None, ge=1990, le=2100)
    mode: Mode | None = None
    #: 武将牌系列 / 武将牌组名称（可以有多个）
    general_sets: list[str] = Field(default_factory=list)
    #: 游戏牌拓展包名称（可以有多个）
    card_sets: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 技能引用


class SkillRef(StrictModel):
    """对某个技能（以及可选的具体版本）的引用。武将版本与卡牌效果共用这一种形状。

    - ``skill_id``：必填，指向 SkillDefinition；
    - ``version_id``：可以为空。为空表示“按该技能的 mode 匹配唯一一条版本”。
    """

    skill_id: Id
    version_id: Id | None = None


# ---------------------------------------------------------------- 武将


class General(StrictModel):
    """**武将个体**。只保存稳定信息：内部 id 与官方武将名。

    ``诸葛亮`` / ``界诸葛亮`` / ``神诸葛亮`` 是**不同**的 General（官方武将名不同）；
    而“标准版诸葛亮”与“国战版诸葛亮”官方名都还是``诸葛亮``，
    它们是同一个 General 的两个 :class:`GeneralVariant`。
    """

    id: Id
    name: Name


class GeneralVariant(StrictModel):
    """某个玩法 / 版本下的武将数据（身份局标准版、国战版、界限突破…）。

    ``mode``：游戏模式；身份局标准版记正常体力（3/4/5），
    国战按规则意义上的阴阳鱼记 1.5 / 2 / 2.5（**不要**把半阴阳鱼记成 3/4/5）。

    来源（实体卡 / 某个产品）由 ``product_id`` 决定，不再重复保存 source。
    """

    id: Id
    general_id: Id
    #: 版本标签（同一武将的多个版本靠它区分，例如 标准版 / 国战版）
    variant: str = "标准版"
    title: str = ""
    general_number: str | None = None
    illustrator: str = ""
    #: 卡图文件名（相对 data/card_images/）
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
    skills: list[SkillRef] = Field(default_factory=list)
    #: 需要人工对照卡图/实物复核的异常（OCR 误读、疑似错字…）
    review_note: str = ""

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

    技能名不是主键；同名技能的不同模式 / 产品 / 时期的文本全部放在 SkillVersion。
    来源与文本都归 SkillVersion，这里不重复保存。
    """

    id: Id
    name: Name
    #: 需要人工复核的异常（例如卡面 OCR 读出的技能名与核对结果不一致）
    review_note: str = ""


class SkillVersion(StrictModel):
    """SkillVersion：某模式 / 某产品下技能的具体规则文本。

    更新技能的正确做法是**新增一条** SkillVersion 并用 ``previous_version_id`` 串链，
    绝不修改旧记录。哪个武将版本 / 哪张卡用哪一版文本，由 ``SkillRef.version_id`` 决定。
    """

    id: Id
    skill_id: Id
    mode: Mode
    text: RuleText
    version_label: str = ""
    product_id: Id | None = None
    previous_version_id: Id | None = None
    source: Source
    #: 需要人工复核的异常（例如文本不是来自卡面、或 OCR 结果未核验）
    review_note: str = ""


# ---------------------------------------------------------------- 卡牌


class CardEffect(StrictModel):
    """卡牌的效果 / 技能。

    - ``text``：卡面效果原文，逐字照抄，允许为空（例如纯技能指示物只有技能引用）；
    - ``skills``：这张卡关联到的技能（0～N 个）。技能文本继续由 Skill / SkillVersion
      维护，**不要把技能说明抄进 ``text``**。
    """

    text: str = ""
    skills: list[SkillRef] = Field(default_factory=list)


class Card(StrictModel):
    """一张有独立规则意义 / 独立卡面的卡（游戏牌与指示物共用这一层）。

    三级分类表达“这张卡是什么”，``card_set`` 表达“它来自哪个拓展包”，两者完全独立。

    同一规则卡因为花色 / 点数不同产生的多份实体印刷**不在这一层建模**：
    当前阶段只维护卡面资料，完整牌堆构成留给以后单独加的 ``deck_entries``。
    """

    id: Id
    category_1: Name
    category_2: Name
    category_3: Name
    name: Name
    #: 卡图文件名（相对 data/card_images/）
    image: str = ""
    #: 所属拓展包；允许为空（跨多个拓展包、或暂时无法确认来源时留空）
    card_set: str | None = None
    effect: CardEffect = Field(default_factory=CardEffect)
    #: 需要人工对照卡图/实物复核的异常
    review_note: str = ""


# ---------------------------------------------------------------- FAQ / 关键词 / 时点


class Faq(StrictModel):
    """规则问答 / 裁定。

    关联的武将 / 技能 / 卡牌 / 关键词不再手工维护：构建索引与渲染详情时
    按实体名称从 question/answer 里自动识别。
    """

    id: Id
    question: RuleText
    answer: RuleText
    mode: Mode = Mode.common
    source: Source


class Keyword(StrictModel):
    """规则关键词，用于规则解释跳转。"""

    id: Id
    name: Name
    definition: RuleText
    mode: Mode = Mode.common
    source: Source


class Timing(StrictModel):
    """规则时点，用于“时点速查”（不是规则结算引擎）。"""

    id: Id
    name: Name
    description: str = ""
    mode: Mode = Mode.common
    source: Source


#: kind -> 模型类
MODEL_BY_KIND: dict[str, type[StrictModel]] = {
    "product": Product,
    "general": General,
    "general_variant": GeneralVariant,
    "skill": Skill,
    "skill_version": SkillVersion,
    "card": Card,
    "faq": Faq,
    "keyword": Keyword,
    "timing": Timing,
}
