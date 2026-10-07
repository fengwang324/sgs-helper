# sgs-helper —— 三国杀线下助手 · 资料库底座

面向线下三国杀（**国战**、**身份局**）的资料库：用 YAML 人工维护权威数据，
一条命令生成 SQLite 运行时数据库，并提供校验、查询与测试。

> 当前阶段的目标只有一个：把**资料库底座**做对。
> 不做复杂前端、不做用户系统、不做云数据库、不接大模型问答、不做规则模拟器。
> （唯一的例外是**只在本机跑**的资料网页：原来的管理页 `sgs_helper/admin/`
> 把手工编辑 YAML 变成表单编辑 YAML，见 [docs/admin.md](docs/admin.md)；
> 现在它上面又多了一层面向日常使用的**本地 Wiki** `/wiki/*`，见 [docs/wiki.md](docs/wiki.md)。）

## 当前数据状态

**全部是真实资料**：规则侧来自规则集 3.0 网页版，武将 / 卡牌侧来自群狼环鼎（`qlhd`）实体卡照片。

| 类型 | 来源 | 状态 |
| --- | --- | --- |
| 技能本体 311 / 技能版本 297 | 群狼环鼎实体卡照片（296 条 `source.type: physical_card`）+ 三国杀移动版官网武将页（1 条 `official_online`） | 卡面文本，待逐条对卡图核验 |
| FAQ / 裁定 85 | 《群狼环鼎规则资料包 v1.0》的人工核对稿（规则手册 19 页照片 + 实卡照片 + 通用规则网页 + B 站 UP 主文章） | 真实文本；`source` 是多行文本，逐条列出来源的 type / title / reference / author / displayed_date |
| 规则关键词 117 | 同上（关键词核对版，依据是手册页码） | 真实定义；关键词不录来源字段 |
| 武将 155 / 武将版本 163 | 群狼环鼎实体卡照片 + [三国杀WIKI（bilibili）](https://wiki.biligame.com/sgs/) | 真实数据；`product_id` 全部指向 `qlhd`，mode 全是国战 |
| 产品 1 / 卡牌 81 | 群狼环鼎（`product_id: qlhd`） | 真实数据；卡牌是一层 `Card`（分类 + 卡名 + 卡图 + 拓展包 + 效果），没有 `source` 字段 |

实测口径（`python scripts/validate_data.py`）：**实体 1218 条 / 0 错误 / 14 警告**
（14 条全是「技能本体没有任何 SkillVersion」；随 `CardPrinting` 一起删除的
「同一产品同花色点数重复」那 46 条警告已消失）。

- 结构化的 `source` 只有 `type` + `reference` 两个字段（**只有 SkillVersion 用**）；展示用的来源名在渲染时由 `reference` 推导
  （`constants.SOURCE_TITLE_BY_REFERENCE` → `SOURCE_LABELS`），不再单独存储。
- `FAQ.source` 是一段**多行文本**：逐条写出来源的 type / title / reference / author / displayed_date；
  `Keyword` 没有来源字段（依据留在录入资料里供人工核对）。
  实体卡类（Product / GeneralVariant / Card）**没有** `source`，来源由 `product_id` / `card_set` 定位。
- 每个技能按模式各留一条版本：国战取群狼环鼎的卡面文本，身份局取**标准版**印次的文本；
  同一技能在其它印次（界限突破、台湾版、铜雀台…）还有不同文本时，再单独补一条 SkillVersion。
- 规则文本逐字照抄网页版，包括站点本身的排版细节；**未按实体卡核验**，
  可以随时在管理页里逐条打开、对着实体卡核对。
- 武将的两层结构见下文《数据模型一览》：`General`（个体）只存 `id / name`，
  模式 / 势力 / 体力 / 称号 / 编号 / 技能都在 `GeneralVariant`（版本）上。
  同一武将的多个版本靠 `variant` 标签与 `mode` 区分（群狼环鼎 / 群狼环鼎·君主 / 群狼环鼎·晋…）。
- 实体卡文本来自卡面照片 OCR，与 WIKI 原文在个别用词上可能不同；
  需要人工对照卡图/实物复核的异常写进正文，或者记在 Git 提交信息里。
- 数据维护与核验方法见《日常操作手册》和 [docs/qlhd-import.md](docs/qlhd-import.md)。

---

## 30 秒上手

```bash
# 在容器里（已挂载 /data）
cd /data/wf/1002/sgs-helper

pip install -r requirements.txt            # 或者：pip install -e ".[dev]"

python scripts/validate_data.py            # 1. 校验 YAML
python scripts/build_db.py                 # 2. 由 YAML 全量重建 build/sgs.db
python -m pytest -q                        # 3. 测试

python scripts/query.py skill 集智         # 4. 查询
python scripts/query.py general 孔融
python scripts/query.py card 杀
python scripts/query.py faq 伤害
python scripts/query.py keyword 濒死
python scripts/query.py product 国战
python scripts/query.py search 集智

# 5. 可选：浏览器里维护资料（只写 YAML，SQLite 由 YAML 重建）
pip install -e ".[admin]"
python scripts/admin.py --port 8000       # 打开 http://127.0.0.1:8000
```

依赖：Python ≥ 3.10、PyYAML、Pydantic v2（测试用 pytest）。不需要任何数据库服务。

## 目录结构

```text
sgs-helper/
├── data/                     # ★ 唯一权威数据源（Git 管理）
│   ├── products/             # 实体产品
│   ├── generals/             # 武将
│   ├── skills/               # SkillDefinition：技能本体
│   ├── skill_versions/       # SkillVersion：技能的具体版本文本
│   ├── cards/                # Card：一层卡牌（分类 + 卡名 + 卡图 + 拓展包 + 效果）
│   ├── faq/                  # FAQ / 裁定
│   ├── keywords/             # 规则关键词
│   └── _templates/           # 新增数据用的模板（目录名以 _ 开头，不参与校验）
├── sgs_helper/               # ★ 核心代码（可被以后的 FastAPI 直接复用）
│   ├── constants.py          # 枚举、中文标签、目录约定
│   ├── schema.py             # Pydantic 模型（YAML 字段即模型字段）
│   ├── loader.py             # YAML 装载 + 来源文件/行号/哈希
│   ├── validate.py           # 模型校验 + 交叉引用 + 一致性检查
│   ├── db.py                 # SQLite 全量重建、FTS5、构建元信息
│   ├── repo.py               # 查询层：只读 SQLite，返回纯 dict
│   ├── render.py             # 把查询结果渲染成人可读文本（CLI 用）
│   ├── cli.py                # validate / build / query / admin 命令行
│   └── admin/                # ★ 本地资料网页（FastAPI + Jinja2，可选依赖）
│       ├── app.py            #   路由与页面装配（管理页 + 挂载 Wiki 路由）
│       ├── wiki.py           #   /wiki/* 本地 Wiki 的页面逻辑
│       ├── store.py          #   读 YAML（快照 + 内存检索）、安全写 YAML、重建 SQLite
│       ├── forms.py          #   低频实体的 generic 表单（由 Pydantic 模型生成）
│       ├── editors.py        #   产品 / 武将 / 卡牌专用录入页的解析与批量改动构造
│       ├── yaml_writer.py    #   可读 YAML 读写（保留注释头/块状标量/原子落盘）
│       ├── templates/        #   Jinja2 模板（wiki/ 子目录是 Wiki 专用）
│       └── static/           #   style.css（管理页）+ wiki.css / wiki-upload.js（Wiki）
├── scripts/                  # 薄包装：validate_data.py / build_db.py / query.py / admin.py
├── tests/                    # pytest 测试（当前 328 个）
├── build/                    # 生成物目录（sgs.db，**不进 Git**）
├── docs/                     # 设计说明、操作手册、路线图、更新记录（updates.md）
├── README.md  pyproject.toml  requirements*.txt  .gitignore
```

## 五条设计原则

1. **YAML 是唯一权威源**，SQLite 只是生成物，可随时由 YAML 完整重建。
2. **不手工修改 SQLite**；`build/sgs.db` 不进 Git（在 `.gitignore` 里）。
3. **技能两层、卡牌一层**：技能定义与版本文本分离；卡牌只有一层 `Card`，**旧版本永不覆盖**。
4. **来源可追溯**：每条数据都记 YAML 文件与行号；规则类实体另带 `source`（只有 `type` + `reference`），构建物里记录 Git commit。
5. **不确定的规则不编造**：拿不准的就别写死，两种说法冲突就都留着（写进正文）。

---

## 数据模型一览

| kind | 目录 | SQLite 表 | 说明 |
| --- | --- | --- | --- |
| product | `data/products/` | `products` | 真实发行的线下产品 |
| general | `data/generals/` | `generals` | **General**：武将个体（只有 `id` / `name`） |
| general_variant | `data/general_variants/` | `general_variants` | **GeneralVariant**：某模式/版本下的武将数据（势力/体力/称号/技能…） |
| skill | `data/skills/` | `skills` | **SkillDefinition**：技能本体 |
| skill_version | `data/skill_versions/` | `skill_versions` | **SkillVersion**：某模式/产品下的技能文本 |
| card | `data/cards/` | `cards` | **Card**：一层卡牌（分类 + 卡名 + 卡图 + 拓展包 + 效果） |
| faq | `data/faq/` | `faqs` | FAQ / 裁定 |
| keyword | `data/keywords/` | `keywords` | 规则关键词 |

公共约定：

- `id`：`^[a-z][a-z0-9_]*$`，带业务前缀（`product_` / `general_` / `skill_` / `card_` / `faq_` / `keyword_`）。
  它是**内部主键**（YAML 文件名 / URL / 外键），管理页会自动生成；实体卡上的编号放 `general_number`（自由字符串）。
- 一个 YAML 文件可以放**一条**记录（推荐）、**一个列表**，或 `items: [ ... ]` 包装。
- 文件放在哪个目录决定它是什么类型；目录名以 `_` 开头的文件/目录会被跳过。
- **写错字段名会直接报错**（模型 `extra="forbid"`），不会静默丢弃。
- `mode`：`common`（通用）/ `guozhan`（国战）/ `identity`（身份局）/ `ol` / `mobile` / `other`；
  **产品与武将的 mode 允许为空**（先录入，以后再补）。
- `Product.general_sets`（武将牌系列）与 `Product.card_sets`（游戏牌拓展包）是两件事，不要混：
  两者分别是 `GeneralVariant.general_set` 与 `Card.card_set` 的**唯一候选来源**（没有独立的「系列」实体，
  也没有单独的系列数据目录）。注意「指示物」是卡牌分类（`category_1`），不是拓展包。
- **武将分两层**：`General`（武将个体）只存 `id / name`；
  模式、势力、体力、性别、称号、编号、产品、武将牌系列、技能全部属于 `GeneralVariant`。
  「诸葛亮 / 界诸葛亮 / 神诸葛亮」官方名不同 → 三个 General；
  「标准版诸葛亮 / 国战诸葛亮」官方名都是「诸葛亮」→ 同一个 General 的两个 Variant。
- `GeneralVariant.hp`（体力）允许为空：身份局标准版记正常体力（3/4/5），
  国战按阴阳鱼记 1.5 / 2 / 2.5。
- `GeneralVariant.skills` 与 `Card.effect.skills` 都是同一种关系列表 `SkillRef`
  `[{skill_id, version_id}]`（`GeneralSkillRef` 已改名 `SkillRef`，武将版本与卡牌效果共用），
  `version_id` 为空表示“按该技能的 `mode` 匹配唯一一条版本”。
- **卡牌只有一层 `Card`**：`category_1/2/3`（是什么）+ `name` + `image`
  + `card_set`（来自哪个拓展包）+ `effect: {text, skills}`。
  同一规则卡因花色 / 点数不同产生的多份实体印刷**不在这里建模**（以后另加 `deck_entries`）；
  三级分类表达“这张卡是什么”，取值**自由填写**（`category_1` 必填，二 / 三级可留空）；
  本项目的习惯是基本牌的 `category_3` 写牌族（杀 / 闪 / 桃 / 酒），具体哪一张写在 `name`（杀 / 火杀 / 雷杀…）。
- `source.type`：`physical_card`（本人实体卡）/ `official_rule` / `official_faq` / `official_online` / `community_db` / `wiki` / `discussion`。
- `source` 只在 `SkillVersion`（结构化 type + reference）与 `FAQ`（多行文本）上是**必填**；
  `Product` / `GeneralVariant` / `Card` **没有** `source` 字段（来源由 `product_id` / `card_set` 定位）。

完整 SQLite 表结构见 [docs/sqlite-schema.md](docs/sqlite-schema.md)。

## SkillDefinition / SkillVersion 如何工作

```text
skill_ji_zhi（技能本体 · data/skills/skill_ji_zhi.yaml）
└── skill_ji_zhi_guozhan_qlhd_v1   国战 · 群狼环鼎 · 当你使用非转化的普通锦囊牌时，你可以摸一张牌。
```

（当前 297 条技能版本**全部是国战**；以后补身份局文本时再新增 `skill_ji_zhi_identity_v1` 之类的版本。
更新技能时新增一条 SkillVersion 并用 `previous_version_id` 串链，见下文《技能平衡更新》。）

- **同名技能在不同模式、不同产品下必须并存**，各写各的 SkillVersion。
- **更新技能 = 新增一条 SkillVersion**：新文件 + `previous_version_id`。
  绝不去改旧文件（旧版本对应实体卡上的旧文本，改了就查不到了）。
- 取哪一版文本由**武将版本显式绑定**，不按日期推导（技能版本没有生效日期 / 生效区间字段，CLI 也没有回看历史日期的开关）：

  1. `GeneralVariant.skills[].version_id` 填了就按它取文本；
  2. 为空时，按该武将版本的 `mode` 匹配该技能**唯一一条**同模式（或 `common`）版本；
  3. 匹配不到（0 条，或多条并列）就表示“这个技能在此武将版本上没有文本”。
- 校验器会额外检查：`previous_version_id` 必须属于同一技能、不能成环、
  `skills[].version_id` 必须属于同一个 `skill_id`。
- CLI 支持 `--json` 输出结构化结果。

## Card 如何工作（单层）

```yaml
# data/cards/card_ding_lan_ye_ming_zhu.yaml —— 定澜夜明珠
id: card_ding_lan_ye_ming_zhu
category_1: 游戏牌
category_2: 装备牌
category_3: 宝物
name: 定澜夜明珠
image: card_ding_lan_ye_ming_zhu_qlhd_diamond_6.jpg
card_set: 九变篇                # 所属拓展包，允许为空
effect:
  text: 锁定技，你视为拥有“制衡”。若你已拥有“制衡”，则改为取消弃置牌数的限制。
  skills:
    - skill_id: skill_zhi_heng
      version_id: skill_zhi_heng_guozhan_qlhd_v1

# data/cards/card_sha.yaml —— 杀（基本牌：三级分类写牌族，具体牌名看 name）
category_1: 游戏牌
category_2: 基本牌
category_3: 杀
name: 杀
image: card_sha_qlhd_spade_4.jpg
card_set: null                  # 允许为空：跨拓展包 / 暂时无法确认来源

# data/cards/card_huo_sha.yaml —— 火杀（先记录，卡图与文本待补）
category_1: 游戏牌
category_2: 基本牌
category_3: 杀
name: 火杀
image: ""
effect:
  text: ""
  skills: []
```

- **只有一层 `Card`**：`category_1/2/3` 表达“这张卡是什么”，`card_set` 表达“它来自哪个拓展包”，
  两者完全独立。**分类是自由填写的，不是白名单**：只有 `category_1` 必填，
  二 / 三级都可以留空，校验器不会要求你把新分类“补进 constants”。
  `constants.CARD_CATEGORY_TREE` 只是**录入候选**：
  `游戏牌 → 基本牌（杀 / 闪 / 桃 / 酒）/ 锦囊牌（非延时锦囊、延时锦囊）/ 装备牌（武器 / 防具 / 坐骑 / 宝物）`；
  「指示物」刻意不预置任何子分类，候选与 Wiki 导航都从**实际录入的数据**生成
  （新增分类保存后自动出现，改名 / 删掉最后一条后自动消失）。
  Wiki 的「游戏牌」与「指示物」两个入口直接按 `category_1` 筛选。
- **`card_set` 是另一个维度**：它表示“来自哪个拓展包”（九变篇 / 势备篇 / 不臣篇 / 君主游戏牌 /
  阵 / 君临天下·权…），允许为空；非空时必须出现在某个产品的 `Product.card_sets` 里。
  **「指示物」是卡牌分类（`category_1`），不是拓展包**（`Product.card_sets` 里的「指示物」已删除）。
- `effect.text` 逐字保存卡面原文，允许为空；`effect.skills` 是 `[{skill_id, version_id}]`，0～N 个；
  技能文本仍由 Skill / SkillVersion 维护，**不要把技能说明抄进 `effect.text`**。
  检索用的规范化正文由 `db.py` 在构建 `search_index` 时**动态生成**，不是 YAML 字段。
- **同一规则卡的花色 / 点数印刷不在这一层建模**：`CardPrinting` 层、`data/card_printings/` 目录、
  相关校验、页面与测试引用已整体删除（完整牌堆构成留给以后单独加的 `deck_entries`）。
- 校验器会告警：基本牌 `category_3` 不是牌族、`card_set` 没有被任何产品声明
  （写「指示物」会给专门提示）、`Card.image` 文件不存在、
  `effect.skills[].skill_id` 不存在或 `version_id` 不属于同一个技能。

---

## 日常操作手册

> 有两条等价的路：**浏览器里用本地管理页录**（推荐，见 [docs/admin.md](docs/admin.md)），
> 或者**复制模板 → 改内容 → validate → build_db → pytest → commit**（模板在 `data/_templates/` 下）。
> 无论走哪条，最终都只改 `data/` 下的 YAML。

### 新增一个实体产品

1. 打开 `/new/product`（或 `cp data/_templates/product.yaml data/products/my_product_2026.yaml`）
2. 日常只需要 5 个字段：`name`、`release_year`、`mode`、`general_sets`、`card_sets`；
   - `general_sets`：该产品包含的**武将牌系列**（国战标准、势备篇、九变篇…），
     同时是武将版本 `general_set` 的候选来源；
   - `card_sets`：该产品包含的**游戏牌拓展包**（国战标准、君主游戏牌、九变篇…），
     同时是卡牌 `card_set` 的候选来源。
   两者是两件事，**不要混**（旧字段 `included_sets` 已删除）。
3. 产品只有这 5 个字段，没有 `series` / `publisher` / `release_date` / `edition` / `source` /
   `verification_status` / `notes`（这些都已从模型删除）。

### 新增一个武将（General + GeneralVariant）

1. 打开 `/new/general`：上半张表填**武将个体**（名字必填、内部 ID），
   下半张表填**版本**（版本标签 `标准版` / `国战版` / …、称号、编号、模式、势力、体力、性别、
   武将牌系列、技能）。官方名不同就是不同武将；同名不同玩法是新开一个版本。
2. **只有 `name` 是必填的**，其余可以先留空，以后再补。
3. **不要在这里写技能文本**：`skills` 只写关系，技能文本由 SkillVersion 提供：

   ```yaml
   # data/generals/general_zhu_ge_liang.yaml —— 武将个体
   id: general_zhu_ge_liang
   name: 诸葛亮

   # data/general_variants/variant_zhu_ge_liang_biao_zhun_ban.yaml —— 版本
   id: variant_zhu_ge_liang_biao_zhun_ban
   general_id: general_zhu_ge_liang
   variant: 标准版
   mode: identity
   faction: 蜀
   hp: 3
   general_set: 标准-灵心慧性
   skills:
     - skill_id: skill_guan_xing
       version_id: skill_guan_xing_identity_v1   # 这张卡印的就是这一版；留空则按 mode 匹配唯一版本
   ```

4. 管理页可以直接搜索已有技能、内联新建技能与技能版本，一次提交完成（只 rebuild 一次）；
   给同一个武将再加版本：进详情页 → 「+ 新增版本」（`/edit/general/<id>?variant=new`）。
5. 校验会告警“某技能在该模式下没有任何 SkillVersion”，提醒你补技能版本；
   “武将还没有任何版本”也会告警。

同名武将在不同产品/版本下会互相覆盖吗？——不会：`id` 唯一即可并存，
**武将的身份键是卡面编号**，`id` 由编号派生（`G.WEI 001` → `general_g_wei_001`），
编号不同就是不同武将，可以并存。

### 新增一个技能

1. `cp data/_templates/skill.yaml data/skills/my_skill.yaml` → 只需 `id` / `name`；
2. 再新增至少一条 SkillVersion（下一步），否则校验会告警“没有任何 SkillVersion”。

### 技能平衡更新时增加新版本

不要改旧文件，**新增一条版本并串链**：

```yaml
# data/skill_versions/my_skill_guozhan_v2.yaml
id: my_skill_guozhan_v2
skill_id: my_skill
mode: guozhan
text: >
  调整后的技能原文……
version_label: 2026 平衡调整后
product_id: null                 # 不绑定具体产品时写 null
previous_version_id: my_skill_guozhan_v1   # ← 指向旧版本
source:
  type: official_rule
  reference: ""
```

要让某个武将版本用上新文本，把它的 `skills[].version_id` 指到新版本；
不指就按 `mode` 匹配该技能唯一一条版本（同模式多条并存时匹配不到，就显示不出文本）。
校验器会检查版本链是否成环、显式绑定的版本是否属于同一个技能。

### 新增一张卡牌

卡牌只有一层，走专用录入页（`/new/card`）或复制模板：

1. 打开 `/new/card`（或 `cp data/_templates/card.yaml data/cards/my_card.yaml`），按录入顺序填：
   卡名 → 一级 / 二级 / 三级分类（联动）→ 拓展包（`card_set`，可空）→ 卡图（`image`，待补可留空）
   → 效果文本（`effect.text`，照抄卡面，可空）→ 关联技能 0～N（`effect.skills`，搜索已有技能、可指定版本）。
2. `card_set` 非空时，必须是某个产品 `Product.card_sets` 里声明过的拓展包名（产品侧补上即可）；
   「指示物」要写到 `category_1`，不能当拓展包。
3. 分类自由填写：只有一级分类必填，二 / 三级可以留空；
   习惯上基本牌的三级分类写牌族（杀 / 闪 / 桃 / 酒），具体是哪一张写在卡名里（校验器不强制）。
4. 卡图放在 `data/card_images/` 下，`image` 只填文件名；待补卡图的（火杀 / 雷杀 / 掠闪）留空。

查询能力：`query.py card 定澜夜明珠` 会显示分类、卡图、拓展包、效果文本与关联技能；
`query.py product qlhd` 会列出「该产品游戏牌（N 条）」（按 `Card.card_set ∈ Product.card_sets` 关联）。

### 增加 FAQ

`cp data/_templates/faq.yaml data/faq/faq_0006.yaml`，填 `question` / `answer` / `variant` /
`source`；`variant` 写版本 / 产品标签（本轮全部是「群狼环鼎」），
`source` 是一段多行文本，逐条写出这条答案的来源属性（type / title / reference / author / displayed_date）。
**关联的武将 / 技能 / 卡牌 / 关键词不用手写**：构建索引与详情页会按实体名称从
question/answer 正文里自动识别。

- 官方结论 → 来源里写 `type: official_faq`；
- 只是社区共识 → 来源里写 `type: community_db`；
- **两边说法不一致 → 不要删掉任何一边**：各留一条记录，
  把“来源 A 说……，来源 B 说……，待核验”写进 `answer` 正文。

### 增加关键词

- `cp data/_templates/keyword.yaml data/keywords/xxx.yaml`，写 `name` / `definition` /
  `variant`（版本 / 产品标签）四个字段；关键词**不录来源字段**。
- 关联不再手工维护：正文里出现其它实体名（武将 / 技能 / 卡牌 / 关键词）时，
  构建索引与详情页会自动识别成可点击链接。

---

## 校验数据

```bash
python scripts/validate_data.py            # 通过返回 0，失败返回 1
python scripts/validate_data.py --strict   # 有警告也算失败
```

输出格式固定为 **文件路径 / 字段 / 原因**：

```text
✗ data/general_variants/variant_zhu_ge_liang_biao_zhun_ban.yaml:20
    variant_zhu_ge_liang_biao_zhun_ban.skills[1].skill_id
    引用的 技能(skill_not_exist) 不存在：data/skills/ 下没有该 id
```

检查项包括：YAML 语法与顶层结构、必填字段、未知字段、id 格式与**重复 id**、
`GeneralVariant.general_id` / `skills[].skill_id` / `skills[].version_id` / `SkillVersion.skill_id` /
`previous_version_id` / `Card.effect.skills[].skill_id` / `product_id` 引用是否存在、
空引用与自引用、`previous_version_id` 是否同技能/是否成环、
`skills[].version_id` 是否属于同一个技能、`GeneralVariant.general_set` 是否在所属产品的
`general_sets` 里、体力步进、武将模式与所属产品是否一致、
基本牌三级分类是否为牌族、`Card.card_set` 是否被某个产品声明、卡图文件是否存在、
武将引用的技能在该模式下是否有版本、技能本体是否缺 SkillVersion、规则类实体是否缺 `source`。

## 重新构建数据库

```bash
python scripts/build_db.py                  # 默认 build/sgs.db，先校验，失败即终止
python scripts/build_db.py --db build/sgs.db --no-fts
```

- 每次构建都**删掉旧库重建**，重复执行不会产生重复数据；
- 构建前自动校验，有错误就退出（返回码 2），不会生成半成品；
- `meta` 表记录 `schema_version`、构建时间、YAML 清单指纹（sha256）、Git commit/branch/dirty、
  各类型实体数量，可用来判断数据库是否过期；
- FTS5 可用时自动建全文索引（对中文做了逐字分词处理），不可用时查询层自动回退 `LIKE` 检索。

## 查询

```bash
python scripts/query.py skill 集智              # 各模式 / 各版本文本 / 来源
python scripts/query.py skill skill_ji_zhi --json
python scripts/query.py general 孔融            # 含“获得技能后的完整技能说明”
python scripts/query.py card 定澜夜明珠         # 分类 / 卡图 / 拓展包 / 效果文本 / 关联技能
python scripts/query.py faq 伤害
python scripts/query.py keyword 濒死
python scripts/query.py product 国战
python scripts/query.py search 集智             # 跨类型检索
python scripts/query.py stats                   # 数据库构建元信息
```

检索顺序：精确名称 → 名称前缀 → 名称包含 → 正文包含；中文短词由 FTS5 兜底。

## 本地资料管理网页（可选，见 docs/admin.md）

不想手改 YAML 时，可以在浏览器里查看 / 搜索 / 新增 / 修改资料：

```bash
pip install -e ".[admin]"                                  # fastapi / uvicorn / jinja2 / python-multipart
uvicorn sgs_helper.admin.app:app --host 127.0.0.1 --port 8000
# 或： python scripts/admin.py --port 8000
```

打开 <http://127.0.0.1:8000>：

- `/` 统一搜索框（9 类实体）+ 数据量 + 当前校验错误；
- `/list/{kind}` 分类列表，可按 `mode` / `product` / `set_name`
  （标签「系列 / 拓展包」：武将侧筛 `general_set`、卡牌侧筛 `card_set`）/ `category_1` / `category_2` 筛选；
- `/view/{kind}/{id}` 详情：技能显示 `SkillDefinition → 各 SkillVersion（模式/文本/来源）`，
  卡牌显示单层 `Card`（一/二/三级分类 · 卡名 · 拓展包 · 效果文本 · 关联技能），
  武将显示 `General → GeneralVariant（每个版本的势力/体力/称号/技能）→ SkillVersion`；
- **`/new/product`、`/new/general`、`/new/card`：产品 / 武将 / 卡牌专用录入页** ——
  产品只要 名称/年份/模式/武将牌/游戏牌；武将**只有名字必填**，
  技能行支持搜索已有技能、内联新建技能、内联新增版本、指定版本、排序；
  卡牌按 卡名 → 三级分类联动 → 拓展包 → 卡图 → 效果文本 → 关联技能 的顺序录；
- `/new/{kind}`、`/edit/{kind}/{id}`：其余低频实体的 generic 表单（字段由 Pydantic 模型生成）；
- 武将页支持「保存并新增下一位」（可继承上一位的产品信息）、「以此武将为模板新增」、
  Ctrl/⌘+S 保存、搜索框 ↑↓/Enter/Esc、sessionStorage 草稿恢复、同名武将与重复版本提醒；
- 删除前检查引用关系（含 `skills[].version_id` 这种嵌套引用），被引用就拒绝删除并列出引用方。

武将录入页一次提交可能同时产生 `1 个 General + 1 个 GeneralVariant + N 个 Skill + N 个 SkillVersion`，
它们由 `AdminStore.save_batch()` 当**一个事务**处理：全部合进内存 → 完整校验 →
全部写盘 → **只 rebuild 一次 SQLite**；任何一步失败则全部回滚、旧数据库保持不变。

页面**不会**直接改 SQLite。保存流程固定为：

```text
浏览器编辑 → 写回对应 YAML → validate → build_db → SQLite 更新
```

先在内存里把改动合进完整数据集跑一遍完整校验，**校验不通过一个字都不落盘**（页面给出
文件 / 字段 / 原因）；写盘用「临时文件 + fsync + os.replace」，重建数据库也先写临时库再原子替换。
编辑页还带内容哈希与 CSRF token：同一条记录被两个标签页同时改、或你手工改过 YAML，提交会被拒绝并提示刷新。

写回的 YAML 仍然是人可读的：字段顺序按模型顺序、长文本用块状标量（不折行）、
文件顶部注释块保留、原文是单条/列表/`items:` 包装就还是哪种结构。
已知限制（记录内部注释、锚点/别名不保留；多记录文件会整文件重写）见
[docs/admin.md](docs/admin.md)。


## 本地 Wiki（`/wiki/*`，见 docs/wiki.md）

在同一个进程、同一个端口下，管理页之上还有一层面向**日常使用**的 Wiki：
浏览资料 + 就地编辑 + UI 原型。启动方式完全一样，打开 <http://127.0.0.1:8000/wiki>：

- **一级入口**：武将 / 技能 / 游戏牌 / 指示物 / FAQ（外加只预留入口的「规则」）
  + 全库搜索（跨四类，按类型分组）；
- 「游戏牌」与「指示物」**直接按 `category_1` 筛选**；
  三级分类回答「这张卡是什么」，与「来自哪个拓展包」（`card_set`）是两套独立信息；
- 详情页之间**互相跳转**：武将 → 技能、技能 → 所属武将、卡牌 → 关联技能、
  各实体 → 相关 FAQ、FAQ → 相关武将 / 技能 / 卡牌 / 关键词；
- **相关 FAQ 是反向索引**：关联沿用「实体名在正文里自动识别」，
  但按命中位置算相关度，避免超长规则专题把关联区刷满（详见 [docs/wiki.md](docs/wiki.md)）；
- **编辑模式**（右上角开关，cookie `sgs_edit`）：默认浏览；开启后列表页有「+新增」、
  详情页有「编辑 / 复制 / 删除」。写入**复用**下面的录入编辑器，不另起一套；
- **图片上传** `POST /api/upload-image` 写进 `data/card_images/`，
  实体里只存**文件名**（相对路径）；武将卡图字段现在也能编辑了；
- **新增分类不需要改代码**：分类树 = 内置树 ∪ 数据里已有的值，
  在录入页直接输入一个新分类名就等于新建分类。

> 当前「指示物」是 **0 条**，这是正常状态（原有指示物数据已删除）。
> 指示物页会显示空状态，并引导从网页录入第一条。


## 测试

```bash
python -m pytest -q          # 328 个测试（核心 194 + 管理页 95 + 本地 Wiki 39）
```

覆盖：YAML 装载（单条/列表/items/语法错误/行号）、模型校验（未知字段、枚举、区间）、
引用与一致性校验（错误与警告）、SQLite 构建（表结构、幂等重建、来源去重、元信息、可追溯列）、
两层关联（技能本体↔版本、同名不覆盖）与单层卡牌（分类 / 拓展包 / 效果技能引用）、
查询（显式绑定版本与按 mode 回退、模式分组、版本链、检索、FAQ 按名称自动关联）、CLI 端到端、
本地管理网页（页面可启动、搜索可用、编辑 YAML 后重建数据库、非法修改被 validate 拦截且不落盘、
新增 SkillVersion 不覆盖旧版本、被引用实体不能删除、YAML 往返无损、CSRF 与并发保护），
以及产品/武将/卡牌专用录入页（武将内部 ID 由卡面编号派生、编号格式、`general_sets` / `card_sets`、
隐藏字段不丢、技能搜索与绑定、显式绑定版本、卡牌三级分类联动与拓展包校验、
批量保存只 rebuild 一次、批量失败一个文件都不写、rebuild 失败全部回滚）。

管理网页的测试全部在 `tmp_path` 的副本上跑，不会碰仓库里真实的 `data/`。

本地 Wiki（`tests/test_wiki.py`，39 条）另外覆盖：五个入口可打开、指示物空状态、
游戏牌与指示物按 `category_1` 互不串台、全库搜索、分页、详情页交叉跳转、
相关 FAQ、通过网页新增第一条指示物（并落到 YAML、换实例重读后仍在）、新增游戏牌、
分类候选含指示物子树、新分类不需改代码、编辑模式开关与 CSRF / 开放重定向防护、
图片上传（白名单、重名去重、只存文件名）、指示物页筛选不串游戏牌拓展包。

---

## Git 工作流

```text
新增 / 修改 YAML  →  validate_data.py  →  build_db.py  →  pytest  →  写 docs/updates.md  →  自查  →  git commit / push
```

- **提交**：`data/` 下的 YAML、`sgs_helper/`、`scripts/`、`tests/`、`docs/`、README、pyproject 等。
- **不提交**：`build/`（含 `build/sgs.db`）、`__pycache__`、`.pytest_cache`、`*.egg-info`、虚拟环境。
- 每次改动 YAML 后请把 `build/sgs.db` 重新生成一遍再提交代码（数据库本身不入库）。

### 每次改动都必须写更新记录并自查

改完先写 [`docs/updates.md`](docs/updates.md)，再做一次自查：

1. **写记录**：目标、改动清单（含顺手做的事）、验证结果，全部如实写进 `updates.md`。
2. **自查翻不偏移**：逐条对照目标，检查这次改动有没有做**多余的事**、有没有偏离方向。
   - 有偏移 → **不 commit**，把多做的部分回退掉，重新做这个任务。
   - 无偏移 → 正常 `commit` + `push`。
3. 「顺带但必要」的改动（例如删字段后必然要跟着删的测试与文档）可以在记录里说明理由后保留，
   但**必须写出来**，不能瞒着当成没发生。

## 后续接微信小程序建议从哪里开始

当前**不要**做登录、用户系统、线上管理后台、MySQL、Elasticsearch、大模型问答。
（`sgs_helper/admin/` 是本机的资料维护网页，不是线上后台：它直接写 YAML 权威源，
没有鉴权与多用户设计。）
需要做 API 时，从 `sgs_helper/repo.py` 上加一层薄 FastAPI 即可：

```text
微信小程序  →  HTTP API（FastAPI）  →  SQLite（build/sgs.db）
```

`repo.py` 的函数已经和要求的接口一一对应，返回的都是纯 `dict`：

| 先期可实现的接口 | 对应函数（已有） |
| --- | --- |
| `GET /api/search?q=集智` | `search(conn, q)` |
| `GET /api/skills/{id}` | `skill_report(conn, id)` |
| `GET /api/skills/{id}/versions` | `skill_report(conn, id)["versions"]` |
| `GET /api/generals/{id}` | `general_report(conn, id)` |
| `GET /api/cards/{id}` | `card_report(conn, id)` |
| `GET /api/faq?q=伤害` | `faq_search(conn, q)` |
| `GET /api/keywords/{id}` | `keyword_report(conn, id)` |

建议的推进顺序：① 把示例数据换成真实数据（最重要的价值来源）→ ② 用 `--json` 校验返回结构 →
③ 加 FastAPI（只读、无鉴权、单进程，读 SQLite 即可）→ ④ 再做基于资料库的 AI QA。

更多说明见 [docs/design.md](docs/design.md)（设计取舍与已知边界）、
[docs/admin.md](docs/admin.md)（本地资料管理网页：入口、写入流程、已知限制）、
[docs/operations.md](docs/operations.md)（维护手册）、[docs/roadmap.md](docs/roadmap.md)（路线图）。

## 已知边界（先不做，记录在案）

- 武将只有 `General` + `GeneralVariant` 两层：同一个武将的不同玩法/版本各写一条 `GeneralVariant`
  （各自带 `mode` / `hp` / `product_id` / `skills`），不需要「武将印刷」层。
- 技能文本没有结构化（没有 effects/targets/conditions 字段）：这是完整的规则引擎，超出当前阶段。
- 来源只记一条：SkillVersion 带结构化的 `source`（type + reference），FAQ 的来源是一段多行文本，
  关键词没有来源字段，实体卡类实体干脆不记 source（武将版本由 `product_id`、卡牌由 `card_set` 定位）；
  需要多来源并列时，目前把两边说法都写进实体自己的正文里；
  以后可以扩展成来源关联表。
