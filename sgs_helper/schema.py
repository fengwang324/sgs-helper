"""Pydantic 数据模型：与 YAML 字段一一对应。

设计目标（个人资料库 / 小程序查询 / 后续 AI 读取，不做通用数据库设计）：

- 只保留实际会**展示、查询、筛选、关联或核验**的字段；
- 能从其它实体推导的数据不重复保存（例如卡牌所属拓展包由 ``card_set`` 表达，不再挂产品）；
- 武将牌：``Product.general_sets`` + ``GeneralVariant.general_set`` 两处维护系列；
- 游戏牌 / 指示物：**一层** :class:`Card`（分类 + 卡名 + 卡图 + 拓展包 + 效果），
  同一规则卡不因为花色点数不同拆成多条；
- 技能版本通过产品定位，不保存来源、重复版本标签或版本演进链；FAQ 保留来源文本。
- 卡牌按牌型使用专属模板：效果是正文，武器单列攻击范围，游戏牌单列牌堆信息。
- 补充裁定归 FAQ，必要的资料备注只在卡牌最外层保存。

约定：

- 所有模型 ``extra="forbid"``：YAML 里写错字段名会直接报错，绝不静默丢弃。
- 枚举字段在模型里保留为字符串（``use_enum_values=True``），落库与返回 JSON 都很直接。
- 多值字段统一用 ``list``，禁止用空字符串冒充空值。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
from typing import Annotated

from .constants import (
    ID_PATTERN,
    Gender,
    Mode,
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
    """武将版本对某个技能（以及可选的具体版本）的引用。

    - ``skill_id``：必填，指向 SkillDefinition；
    - ``version_id``：可以为空。为空表示“按该技能的 mode 匹配唯一一条版本”。
    """

    skill_id: Id
    version_id: Id | None = None


# ---------------------------------------------------------------- 武将


class General(StrictModel):
    """**独立武将记录**。本体保存内部 id 与武将名。

    名称不是身份键：同名但卡面编号或称号不同的武将，保留独立 General 与各自
    :class:`GeneralVariant`，不能仅按名字合并。编号、称号与技能仍在版本中维护。
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
    文本归 SkillVersion，这里不重复保存。
    """

    id: Id
    name: Name


class SkillVersion(StrictModel):
    """SkillVersion：某模式 / 某产品下技能的具体规则文本。

    武将使用哪一版文本，由 ``SkillRef.version_id`` 决定。
    产品名称通过 ``product_id`` 取得，不重复保存标签、来源或演进链。
    """

    id: Id
    skill_id: Id
    mode: Mode
    text: RuleText
    product_id: Id | None = None


# ---------------------------------------------------------------- 卡牌


class CardUseMethod(StrictModel):
    """一种使用方法。各字段都是原文文本，允许 X、条件、多个目标和暂缺资料。"""

    label: str = ""
    timing: str = ""
    target: str = ""
    effect: str = ""


class GameCardRules(StrictModel):
    """游戏牌专用规则。通用装备规则由 Keyword 维护，不在每张卡重复。

    基本牌 / 锦囊牌使用 use_methods；装备能力使用 Card.effect，
    武器攻击范围、牌堆资料与必要备注在卡牌最外层保存。
    所有标量均为文本。分类仍自由填写，这些字段是可选内容而非分类白名单。
    """

    use_methods: list[CardUseMethod] = Field(default_factory=list)


class Card(StrictModel):
    """一张有独立规则意义 / 独立卡面的卡（游戏牌与指示物共用这一层）。

    三级分类表达“这张卡是什么”，``card_set`` 表达“它来自哪个拓展包”，两者完全独立。

    分类是**自由填写**的：``category_1`` 必填（它决定这张卡属于「游戏牌」还是「指示物」入口），
    ``category_2`` / ``category_3`` 都可以留空（例如只填一级，或只填一 / 二级）；
    取值不受任何预设白名单约束，新增分类不需要改代码。

    同一规则卡因为花色 / 点数不同产生的多份实体印刷**不在这一层建模**：
    牌堆数量、花色与点数用 ``deck_info`` 文本维护，不拆成多条卡牌。
    """

    id: Id
    category_1: Name
    category_2: str = ""
    category_3: str = ""
    name: Name
    #: 卡图文件名（相对 data/card_images/）
    image: str = ""
    #: 所属拓展包；允许为空（跨多个拓展包、或暂时无法确认来源时留空）
    card_set: str | None = None
    effect: str = ""
    #: 武器的攻击范围，允许 X、条件等原文文本。
    attack_range: str = ""
    #: 游戏牌的牌堆数量、花色及点数；空字符串表示待录入，指示物不使用。
    deck_info: str | None = None
    #: 可选的资料备注；补充裁定归 FAQ。
    note: str = ""
    #: 基本牌 / 锦囊牌的使用方法；装备及指示物不填写。
    rules: GameCardRules | None = None

    @field_validator("rules")
    @classmethod
    def _game_rules_only(cls, value, info):
        if value is not None and info.data.get("category_1") != "游戏牌":
            raise ValueError("rules 是游戏牌专用字段；指示物及其他自由分类使用 effect")
        return value

    @model_validator(mode="after")
    def _specialized_fields(self):
        if self.category_1 == "游戏牌":
            if self.deck_info is None:
                self.deck_info = ""
        else:
            if self.deck_info is not None:
                raise ValueError("deck_info 是游戏牌专用字段；指示物不填写牌堆信息")
            if self.card_set:
                raise ValueError("card_set 是游戏牌专用字段；指示物不填写拓展包")
            if self.attack_range:
                raise ValueError("attack_range 是游戏牌专用字段；指示物不填写攻击范围")
        if self.category_2 == "装备牌" and self.rules is not None:
            raise ValueError("装备牌使用 effect 和外层 attack_range，不填写 rules")
        if self.attack_range and self.category_2 == "装备牌" and self.category_3 != "武器":
            raise ValueError("attack_range 只用于武器；坐骑距离写在 effect 中")
        return self


# ---------------------------------------------------------------- FAQ / 关键词


class Faq(StrictModel):
    """规则问答 / 裁定。

    与 SkillVersion 不同，FAQ 的 ``source`` 是一段**多行文本**：逐条写出来源的
    type / title / reference / author / displayed_date（本轮按核对资料原样写入，
    以后需要结构化再单独迁移）。

    关联的武将 / 技能 / 卡牌 / 关键词不再手工维护：构建索引与渲染详情时
    按实体名称从 question/answer 里自动识别。
    """

    id: Id
    question: RuleText
    answer: RuleText
    #: 变体 / 版本标签（本轮全部是产品名「群狼环鼎」）
    variant: str = ""
    #: 来源：多行文本，逐条列出来源的各个属性
    source: RuleText


class Keyword(StrictModel):
    """规则关键词，用于规则解释跳转。

    只有 id / name / definition / variant 四个字段：本轮关键词核对版的依据
    （手册页码）不录入，留在录入资料里供人工核对。
    """

    id: Id
    name: Name
    definition: RuleText
    #: 变体 / 版本标签（本轮全部是产品名「群狼环鼎」）
    variant: str = ""


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
}
