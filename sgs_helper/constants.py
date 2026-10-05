"""集中定义枚举、中文标签与目录约定。

所有取值只在这里定义一次，校验、建库、查询、未来的 FastAPI 都复用。
新增模式 / 来源类型 / 卡牌类型时，只改这个文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------- 通用约束

#: 所有实体 id 的格式：小写字母开头，只含小写字母、数字、下划线。
#: 约定带业务前缀：product_ / general_ / skill_ / card_ / faq_ / keyword_ / timing_
ID_PATTERN = r"^[a-z][a-z0-9_]*$"

#: 日期在 YAML 中写成 ``2025-06-01``；生效区间按**日期闭区间**处理
#: （effective_from <= as_of <= effective_to），未填写的一端视为无穷。
DATE_FORMAT = "YYYY-MM-DD"


class Mode(str, Enum):
    """游戏模式。``common`` 表示跨模式通用（例如基础规则关键词）。"""

    common = "common"
    guozhan = "guozhan"      # 国战
    identity = "identity"    # 身份局
    ol = "ol"                # 官方线上版
    mobile = "mobile"        # 移动版 / 手杀
    other = "other"          # 其他线下变体


MODE_LABELS: dict[str, str] = {
    "common": "通用",
    "guozhan": "国战",
    "identity": "身份局",
    "ol": "OL",
    "mobile": "移动版",
    "other": "其他",
}

#: 判断“当前有效版本”时，具体模式优先于 common（数值越大越优先）。
MODE_SPECIFICITY: dict[str, int] = {
    "common": 0,
    "guozhan": 1,
    "identity": 1,
    "ol": 1,
    "mobile": 1,
    "other": 1,
}


class VerificationStatus(str, Enum):
    """核验状态。不做复杂可信度评分；冲突时两边都保留并标记 conflict。"""

    unverified = "unverified"                # 未核验
    community = "community"                  # 仅来自社区资料
    official = "official"                    # 已有官方依据
    physical_verified = "physical_verified"  # 已按实体卡核验
    conflict = "conflict"                    # 不同来源存在冲突


VERIFICATION_LABELS: dict[str, str] = {
    "unverified": "未核验",
    "community": "社区资料",
    "official": "官方依据",
    "physical_verified": "实体卡已核验",
    "conflict": "来源冲突",
}


class SourceType(str, Enum):
    """资料来源类型。"""

    physical_card = "physical_card"      # 本人实体卡
    official_rule = "official_rule"      # 官方规则
    official_faq = "official_faq"        # 官方 FAQ / 官方裁定
    official_online = "official_online"  # 官方线上资料
    community_db = "community_db"        # 社区资料库
    wiki = "wiki"                        # wiki
    discussion = "discussion"            # 玩家讨论


SOURCE_LABELS: dict[str, str] = {
    "physical_card": "本人实体卡",
    "official_rule": "官方规则",
    "official_faq": "官方 FAQ/裁定",
    "official_online": "官方线上资料",
    "community_db": "社区资料库",
    "wiki": "wiki",
    "discussion": "玩家讨论",
}


class CardType(str, Enum):
    basic = "basic"                  # 基本牌
    trick = "trick"                  # 普通锦囊
    delayed_trick = "delayed_trick"  # 延时锦囊
    equipment = "equipment"          # 装备牌
    other = "other"


CARD_TYPE_LABELS: dict[str, str] = {
    "basic": "基本牌",
    "trick": "普通锦囊",
    "delayed_trick": "延时锦囊",
    "equipment": "装备牌",
    "other": "其他",
}


class Suit(str, Enum):
    spade = "spade"      # 黑桃
    heart = "heart"      # 红桃
    club = "club"        # 梅花
    diamond = "diamond"  # 方块
    none = "none"        # 无花色


SUIT_LABELS: dict[str, str] = {
    "spade": "黑桃",
    "heart": "红桃",
    "club": "梅花",
    "diamond": "方块",
    "none": "无花色",
}


class Gender(str, Enum):
    male = "male"
    female = "female"
    unknown = "unknown"


GENDER_LABELS: dict[str, str] = {
    "male": "男",
    "female": "女",
    "unknown": "未知",
}


# ---------------------------------------------------------------- 目录约定


@dataclass(frozen=True)
class KindSpec:
    """一种实体类型的目录 / 表名 / 中文标签约定。"""

    kind: str        # 内部键，也是 id 前缀
    table: str       # SQLite 表名
    dirname: str     # data/ 下的目录名
    label: str       # 中文名
    id_prefix: str   # id 前缀（文档与生成用，不做硬校验）


KINDS: tuple[KindSpec, ...] = (
    KindSpec("product", "products", "products", "实体产品", "product_"),
    KindSpec("general_set", "general_sets", "general_sets", "武将牌系列", "set_"),
    KindSpec("general", "generals", "generals", "武将", "general_"),
    KindSpec("general_variant", "general_variants", "general_variants", "武将版本", "variant_"),
    KindSpec("skill", "skills", "skills", "技能本体", "skill_"),
    KindSpec("skill_version", "skill_versions", "skill_versions", "技能版本", "skill_"),
    KindSpec("card", "cards", "cards", "卡牌定义", "card_"),
    KindSpec("card_printing", "card_printings", "card_printings", "实体卡印刷", "card_"),
    KindSpec("faq", "faqs", "faq", "FAQ/裁定", "faq_"),
    KindSpec("keyword", "keywords", "keywords", "规则关键词", "keyword_"),
    KindSpec("timing", "timings", "timings", "时点", "timing_"),
)

KIND_BY_NAME: dict[str, KindSpec] = {k.kind: k for k in KINDS}
KIND_BY_DIRNAME: dict[str, KindSpec] = {k.dirname: k for k in KINDS}
KIND_BY_TABLE: dict[str, KindSpec] = {k.table: k for k in KINDS}
KIND_ORDER: tuple[str, ...] = tuple(k.kind for k in KINDS)

#: 展示顺序：具体模式在前，通用规则在后
MODE_ORDER: tuple[str, ...] = ("guozhan", "identity", "ol", "mobile", "other", "common")


def mode_sort_key(mode: str) -> tuple[int, str]:
    try:
        return (MODE_ORDER.index(mode), mode)
    except ValueError:
        return (len(MODE_ORDER), mode)
