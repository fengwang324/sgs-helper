# sgs-helper —— 三国杀线下助手 · 资料库底座

面向线下三国杀（**国战**、**身份局**）的资料库：用 YAML 人工维护权威数据，
一条命令生成 SQLite 运行时数据库，并提供校验、查询与测试。

> 当前阶段的目标只有一个：把**资料库底座**做对。
> 不做复杂前端、不做用户系统、不做云数据库、不接大模型问答、不做规则模拟器。
> （唯一的例外是一个**只在本机跑**的资料维护网页 `sgs_helper/admin/`：
> 它没有前端框架、没有登录，只是把手工编辑 YAML 变成表单编辑 YAML，见 [docs/admin.md](docs/admin.md)。）

## 当前数据状态

**规则侧 + 武将侧已经是真实资料**（规则集 3.0 + 两个 wiki），**产品 / 卡牌侧仍是 SAMPLE 占位**：

| 类型 | 来源 | 状态 |
| --- | --- | --- |
| 技能本体 308 / 技能版本 397 | gltjk.com 网页版《三国杀官方规则集 3.0》 | 真实文本，`verification_status: community`（社区网页版，未按实体卡核验） |
| FAQ / 裁定 83 | 同上（规则专题与流程章节 + 武将页的 ◆ 裁定） | 同上 |
| 规则关键词 126 / 时点 30 | 同上（用语、游戏的流程） | 同上 |
| 武将 26 / 武将版本 26 | [三国杀WIKI（bilibili）](https://wiki.biligame.com/sgs/)，标准包部分用[萌娘百科·三国杀](https://mzh.moegirl.org.cn/%E4%B8%89%E5%9B%BD%E6%9D%80)核对名单 | 真实数据，`verification_status: community`；**只有标准包（标准身份版）**，每个武将一条 `标准版` Variant |
| 产品 3 / 卡牌 4 / 实体卡印刷 6 | 仓库自带示例 | **SAMPLE 占位**，名称带 `【SAMPLE】` 前缀，待用实体卡替换 |

- 规则侧来源统一写成：`source.type: official_rule`、`source.title: 三国杀官方规则集 3.0`、
  `source.reference: https://gltjk.com/sanguosha/rules/`（`source.notes` 里注明这是社区网页版）。
- 每个技能按模式各留一条版本：身份局取**标准版**印次的文本，国战取**国战标准版**；
  同一技能在其它印次（界限突破、台湾版、铜雀台、国战·阵/势…）还有不同文本时，
  会写在该版本的 `notes` 里，需要时再单独补一条 SkillVersion。
- 卡面/规则文本逐字照抄网页版，包括站点本身的排版细节；**未按实体卡核验**，
  所以统一是 `community`，可以随时在管理页里逐条核对升级成 `physical_verified`。
- 武将的两层结构见下文《数据模型一览》：`General`（个体）只存名字/别名，
  模式 / 势力 / 体力 / 称号 / 编号 / 技能都在 `GeneralVariant`（版本）上。
  本轮只录**标准身份版**：身份局体力记 3/4/5；国战等版本以后再加 Variant。
- 技能文本目前用的还是规则集（凌天翼 3.0）的规范化表述，与 WIKI 卡面原文在个别用词上可能不同，
  每个武将版本的 `notes` 里都写了“待按实体卡核验”。
- 产品 / 卡牌的替换方法见《日常操作手册》。在替换完成前，**不要把 SAMPLE 文本当成规则结论**。

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
python scripts/query.py keyword 濒死状态
python scripts/query.py timing 伤害结算
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
│   ├── cards/                # CardDefinition：规则意义上的卡牌
│   ├── card_printings/       # CardPrinting：某产品实际印刷的一张实体卡
│   ├── faq/                  # FAQ / 裁定
│   ├── keywords/             # 规则关键词
│   ├── timings/              # 时点
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
│   └── admin/                # ★ 本地资料管理网页（FastAPI + Jinja2，可选依赖）
│       ├── app.py            #   路由与页面装配
│       ├── store.py          #   读 YAML（快照 + 内存检索）、安全写 YAML、重建 SQLite
│       ├── forms.py          #   低频实体的 generic 表单（由 Pydantic 模型生成）
│       ├── editors.py        #   产品 / 武将专用录入页的解析与批量改动构造
│       ├── yaml_writer.py    #   可读 YAML 读写（保留注释头/块状标量/原子落盘）
│       ├── templates/        #   Jinja2 模板
│       └── static/           #   一个 CSS + 一个很小的 JS
├── scripts/                  # 薄包装：validate_data.py / build_db.py / query.py / admin.py
├── tests/                    # pytest 测试（108 个）
├── build/                    # 生成物目录（sgs.db，**不进 Git**）
├── docs/                     # 设计说明、操作手册、路线图
├── README.md  pyproject.toml  requirements*.txt  .gitignore
```

## 五条设计原则

1. **YAML 是唯一权威源**，SQLite 只是生成物，可随时由 YAML 完整重建。
2. **不手工修改 SQLite**；`build/sgs.db` 不进 Git（在 `.gitignore` 里）。
3. **技能两层、卡牌两层**：定义与版本/印刷分离，**旧版本永不覆盖**。
4. **来源可追溯**：每条数据都带 `source`；构建物里记录 YAML 文件、行号与 Git commit。
5. **不确定的规则不编造**：拿不准就标 `unverified` / 冲突就标 `conflict`，两边都留着。

---

## 数据模型一览

| kind | 目录 | SQLite 表 | 说明 |
| --- | --- | --- | --- |
| product | `data/products/` | `products` | 真实发行的线下产品 |
| general | `data/generals/` | `generals` | **General**：武将个体（只有名字/别名/备注） |
| general_variant | `data/general_variants/` | `general_variants` | **GeneralVariant**：某模式/版本下的武将数据（势力/体力/称号/技能…） |
| skill | `data/skills/` | `skills` | **SkillDefinition**：技能本体 |
| skill_version | `data/skill_versions/` | `skill_versions` | **SkillVersion**：某模式/产品/时期的文本 |
| card | `data/cards/` | `cards` | **CardDefinition**：规则意义上的卡牌 |
| card_printing | `data/card_printings/` | `card_printings` | **CardPrinting**：某产品实际印刷的一张卡 |
| faq | `data/faq/` | `faqs` | FAQ / 裁定 |
| keyword | `data/keywords/` | `keywords` | 规则关键词 |
| timing | `data/timings/` | `timings` | 时点 |

公共约定：

- `id`：`^[a-z][a-z0-9_]*$`，带业务前缀（`product_` / `general_` / `skill_` / `card_` / `faq_` / `keyword_` / `timing_`）。
  它是**内部主键**（YAML 文件名 / URL / 外键），管理页会自动生成；实体卡上的编号放 `general_number`（自由字符串）。
- 一个 YAML 文件可以放**一条**记录（推荐）、**一个列表**，或 `items: [ ... ]` 包装。
- 文件放在哪个目录决定它是什么类型；目录名以 `_` 开头的文件/目录会被跳过。
- **写错字段名会直接报错**（模型 `extra="forbid"`），不会静默丢弃。
- `mode`：`common`（通用）/ `guozhan`（国战）/ `identity`（身份局）/ `ol` / `mobile` / `other`；
  **产品与武将的 mode 允许为空**（先录入，以后再补）。
- `Product.general_sets`（武将牌系列）与 `Product.card_sets`（游戏牌牌堆）是两件事，不要混。
- **武将分两层**：`General`（武将个体）只存 `id / name / aliases / notes`；
  模式、势力、体力、性别、称号、编号、产品、武将牌系列、技能全部属于 `GeneralVariant`。
  「诸葛亮 / 界诸葛亮 / 神诸葛亮」官方名不同 → 三个 General；
  「标准版诸葛亮 / 国战诸葛亮」官方名都是「诸葛亮」→ 同一个 General 的两个 Variant。
- `GeneralVariant.hp`（体力）允许为空：身份局标准版记正常体力（3/4/5），
  国战按阴阳鱼记 1.5 / 2 / 2.5；`GeneralVariant.skills` 是关系列表
  `[{skill_id, version_id}]`，`version_id` 为空表示“按模式+生效区间自动推导”。
- `verification_status`：`unverified` / `community` / `official` / `physical_verified` / `conflict`。
- `source.type`：`physical_card`（本人实体卡）/ `official_rule` / `official_faq` / `official_online` / `community_db` / `wiki` / `discussion`。
- `aliases` 用于别名检索；`source` 在技能版本、实体卡、FAQ、关键词、时点是**必填**，
  产品 / 武将 / 技能本体允许为空（缺来源只会告警，不阻塞录入）。

完整 SQLite 表结构见 [docs/sqlite-schema.md](docs/sqlite-schema.md)。

## SkillDefinition / SkillVersion 如何工作

```text
skill_ji_zhi（技能本体 · data/skills/skill_ji_zhi.yaml）
├── skill_ji_zhi_guozhan_v1     国战 · 国战标准版印次 · 每当你使用非转化的非延时类锦囊牌时…
└── skill_ji_zhi_identity_v1    身份局 · 标准版印次 · 每当你使用锦囊牌时…
```

（更新技能时新增一条 SkillVersion 并用 `previous_version_id` 串链，见下文《技能平衡更新》。）

- **同名技能在不同模式、不同产品、不同时期下必须并存**，各写各的 SkillVersion。
- **更新技能 = 新增一条 SkillVersion**：新文件 + `previous_version_id` + `effective_from` + `change_note`。
  绝不去改旧文件（旧版本对应实体卡上的旧文本，改了就查不到了）。
- 查询时按 `mode + effective_from + effective_to` **自动推导“当前有效版本”**，
  不使用容易忘记维护的 `current: true`：

  1. 先筛 `skill_id` 相同、`mode` 为「请求模式」或 `common` 的版本；
  2. 生效区间按**日期闭区间**判断：`effective_from ≤ as_of ≤ effective_to`，未填的一端视为无穷；
  3. 具体模式优先于 `common`；再取 `effective_from` 最晚者；
  4. 若仍有并列，判定为**有歧义**：全部列出并提示，绝不静默挑一个。
- 校验器会额外检查：`previous_version_id` 必须属于同一技能、不能成环、
  同一 `(skill_id, mode, 产品范围)` 内生效区间不能重叠（重叠会告警）。
- CLI 支持 `--as-of 2024-01-01` 回看历史，`--json` 输出结构化结果。

## CardDefinition / CardPrinting 如何工作

```text
card_sha（“杀”这个概念 · data/cards/sha.yaml）
├── card_sha_sample_guozhan_spade_7    国战 · product_guozhan_sample_2024 · 黑桃7 · 角标 君
├── card_sha_sample_guozhan_diamond_9  国战 · product_guozhan_sample_2024 · 方块9 · 角标 君
└── card_sha_sample_identity_spade_8   身份局 · product_identity_sample_2025 · 黑桃8
```

- `CardPrinting.product_id` **必填**：必须能回答“这张实体卡来自哪个线下产品”。
- `printed_text` 逐字保存卡面原文；需要检索文本时另填 `normalized_text`，**不许覆盖** `printed_text`。
- `bottom_right_mark` 单独成列（不写进 notes），方便以后查“所有带某个角标的卡”、
  “某个牌堆是否混入其他产品的卡”。第一版保持简单字符串，需要时再扩展成结构化对象。
- `rank` 可以写 `7`，也可以按卡面写 `A/J/Q/K`（会归一化成 `1/11/12/13` 入库）。
- 校验器会告警：同一产品同一牌堆下出现多张相同花色点数的实体卡（可能是重复登记或混牌）。

---

## 日常操作手册

> 有两条等价的路：**浏览器里用本地管理页录**（推荐，见 [docs/admin.md](docs/admin.md)），
> 或者**复制模板 → 改内容 → validate → build_db → pytest → commit**（模板在 `data/_templates/` 下）。
> 无论走哪条，最终都只改 `data/` 下的 YAML。

### 新增一个实体产品

1. 打开 `/new/product`（或 `cp data/_templates/product.yaml data/products/my_product_2026.yaml`）
2. 日常只需要 5 个字段：`name`、`release_year`、`mode`、`general_sets`、`card_sets`；
   - `general_sets`：该产品包含的**武将牌系列**（国战标准、势备篇、九变篇…）；
   - `card_sets`：该产品包含的**游戏牌牌堆**（标准牌堆、君主游戏牌…）。
   两者是两件事，**不要混**（旧字段 `included_sets` 已删除）。
3. `publisher` / `edition` / `source` / `verification_status` 等属于“高级信息”，可以留空。

### 新增一个武将（General + GeneralVariant）

1. 打开 `/new/general`：上半张表填**武将个体**（名字必填、别名、内部 ID），
   下半张表填**版本**（版本标签 `标准版` / `国战版` / …、称号、编号、模式、势力、体力、性别、
   武将牌系列、技能）。官方名不同就是不同武将；同名不同玩法是新开一个版本。
2. **只有 `name` 是必填的**，其余可以先留空，以后再补。
3. **不要在这里写技能文本**：`skills` 只写关系，技能文本由 SkillVersion 提供：

   ```yaml
   # data/generals/general_zhu_ge_liang.yaml —— 武将个体
   id: general_zhu_ge_liang
   name: 诸葛亮
   aliases: [老诸葛, 空城诸葛]

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
       version_id: skill_guan_xing_identity_v1   # 这张卡印的就是这一版；留空则自动推导
   ```

4. 管理页可以直接搜索已有技能、内联新建技能与技能版本，一次提交完成（只 rebuild 一次）；
   给同一个武将再加版本：进详情页 → 「+ 新增版本」（`/edit/general/<id>?variant=new`）。
5. 校验会告警“某技能在该模式下没有任何 SkillVersion”，提醒你补技能版本；
   “武将还没有任何版本”也会告警。

同名武将在不同产品/版本下会互相覆盖吗？——不会：`id` 唯一即可并存，
管理页会自动生成内部 `id`，卡面编号放在版本的 `general_number` 里。

### 新增一个技能

1. `cp data/_templates/skill.yaml data/skills/my_skill.yaml` → 只需 `id` / `name` / `aliases` / `notes`；
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
effective_from: 2026-01-01
effective_to: null               # 空 = 至今有效
previous_version_id: my_skill_guozhan_v1   # ← 指向旧版本
change_note: >
  把“……”改为“……”；依据：<官方公告标题/链接>。
source:
  type: official_rule
  title: 某次平衡调整公告
  reference: ""
verification_status: official
```

旧版本 `effective_to` 一并补上截止日期（也可以留空，由推导规则取最晚的生效版本）。
校验器会检查区间是否重叠、版本链是否成环。

### 新增一张实体卡

先有 CardDefinition，再登记印刷：

1. `cp data/_templates/card.yaml data/cards/my_card.yaml` → `id` / `name` / `card_type` / `subtype` / `related_keywords`；
2. `cp data/_templates/card_printing.yaml data/card_printings/my_card_guozhan_spade_7.yaml`
3. 填写 `card_id`、`name_printed`、`product_id`（必填）、`card_set`、`suit`、`rank`、`quantity`、
   `printed_text`（照抄卡面）、`bottom_right_mark`、`card_number`、`source`。

### 记录右下角角标

```yaml
bottom_right_mark: 君       # 单独字段，别写进 notes
```

查询能力：`query.py card 杀` 会显示每张印刷的角标；`query.py product 国战` 会汇总该产品的角标分布。
以后如果要按角标反查产品，直接查 `card_printings.bottom_right_mark`（已建索引）。

### 增加 FAQ

`cp data/_templates/faq.yaml data/faq/faq_0006.yaml`，填 `question` / `answer` / `mode` /
`related_*`（武将/技能/卡牌/关键词/时点，用 id 列表）以及 `source` + `verification_status`。

- 官方结论 → `verification_status: official`，`source.type: official_faq`；
- 只是社区共识 → `community` / `source.type: community_db`；
- **两边说法不一致 → 不要删掉任何一边**：保留各条记录，把冲突条目标 `conflict`，
  并在 `notes` 写清“来源 A 说……，来源 B 说……，待核验”。

### 增加关键词 / 时点

- 关键词：`cp data/_templates/keyword.yaml data/keywords/xxx.yaml`，写 `definition`，
  用 `related_keyword_ids` / `related_timing_ids` 做跳转。
- 时点：`cp data/_templates/timing.yaml data/timings/xxx.yaml`，用 `parent_id` /
  `previous_ids` / `next_ids` 描述先后关系（校验器会检查成环与双向一致性）。

---

## 校验数据

```bash
python scripts/validate_data.py            # 通过返回 0，失败返回 1
python scripts/validate_data.py --strict   # 有警告也算失败
```

输出格式固定为 **文件路径 / 字段 / 原因**：

```text
✗ data/generals/kongrong_sample_guozhan.yaml:20
    general_kongrong_sample_guozhan.skills[1].skill_id
    引用的 技能(skill_not_exist) 不存在：data/skills/ 下没有该 id
```

检查项包括：YAML 语法与顶层结构、必填字段、未知字段、id 格式与**重复 id**、
`General.skills[].skill_id` / `skills[].version_id` / `SkillVersion.skill_id` / `previous_version_id` /
`CardPrinting.card_id` / `product_id` / FAQ 全部 `related_*` / Keyword / Timing 引用是否存在、
空引用与自引用、`previous_version_id` 是否同技能/是否成环/时间是否倒退、
同一技能同一模式生效区间是否重叠、时点 parent 链是否成环、prev/next 是否双向一致、
体力步进、产品与模式是否一致、同一产品重复花色点数、技能是否缺版本、是否缺 source。

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
python scripts/query.py skill 集智              # 各模式 / 当前有效版本 / 历史版本 / 文本 / 来源 / 变更说明
python scripts/query.py skill 集智 --as-of 2024-01-01
python scripts/query.py skill skill_ji_zhi --json
python scripts/query.py general 孔融            # 含“获得技能后的完整技能说明”
python scripts/query.py card 杀                 # 定义 + 各产品印刷（花色/点数/角标/卡面原文）
python scripts/query.py faq 伤害
python scripts/query.py keyword 濒死状态
python scripts/query.py timing 伤害结算
python scripts/query.py product 国战
python scripts/query.py search 集智             # 跨类型检索
python scripts/query.py stats                   # 数据库构建元信息
```

检索顺序：精确名称 → 别名 → 名称前缀/包含 → 正文包含；中文短词由 FTS5 兜底。

## 本地资料管理网页（可选，见 docs/admin.md）

不想手改 YAML 时，可以在浏览器里查看 / 搜索 / 新增 / 修改资料：

```bash
pip install -e ".[admin]"                                  # fastapi / uvicorn / jinja2 / python-multipart
uvicorn sgs_helper.admin.app:app --host 127.0.0.1 --port 8000
# 或： python scripts/admin.py --port 8000
```

打开 <http://127.0.0.1:8000>：

- `/` 统一搜索框（10 类实体）+ 数据量与核验状态分布 + 当前校验错误；
- `/list/{kind}` 分类列表，可按 `mode` / `verification_status` / `product` / `general_set` 筛选；
- `/view/{kind}/{id}` 详情：技能显示 `SkillDefinition → 当前版本 → 历史版本链`，
  卡牌显示 `CardDefinition → 所有 CardPrinting（产品/花色/点数/卡面文本/右下角角标）`，
  武将显示 `General → GeneralVariant（每个版本的势力/体力/称号/技能）→ SkillVersion`；
- **`/new/product`、`/new/general`：产品 / 武将专用录入页** ——
  产品只要 名称/年份/模式/武将牌/游戏牌；武将**只有名字必填**，
  技能行支持搜索已有技能、内联新建技能、内联新增版本、指定版本、排序；
- `/new/{kind}`、`/edit/{kind}/{id}`：其余低频实体的 generic 表单（字段由 Pydantic 模型生成）；
- 武将页支持「保存并新增下一位」（可继承上一位的产品信息）、「以此武将为模板新增」、
  Ctrl/⌘+S 保存、搜索框 ↑↓/Enter/Esc、sessionStorage 草稿恢复、同名武将与重复版本提醒；
- `/review` 汇总 `unverified` / `community` / `conflict`，方便拿实体卡逐条核验；
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


## 测试

```bash
python -m pytest -q          # 186 个测试
```

覆盖：YAML 装载（单条/列表/items/语法错误/行号）、模型校验（未知字段、枚举、区间、rank 归一化）、
引用与一致性校验（错误与警告）、SQLite 构建（表结构、幂等重建、来源去重、元信息、可追溯列）、
两层关联（技能本体↔版本、卡牌定义↔印刷、同名不覆盖）、
查询（当前有效版本按日期推导、模式分组、历史链、检索、FAQ 关联）、CLI 端到端、
本地管理网页（页面可启动、搜索可用、编辑 YAML 后重建数据库、非法修改被 validate 拦截且不落盘、
新增 SkillVersion 不覆盖旧版本、被引用实体不能删除、YAML 往返无损、CSRF 与并发保护），
以及产品/武将专用录入页（只填名字也能建、自动生成内部 ID、编号格式、general_sets/card_sets、
隐藏字段不丢、技能搜索与绑定、显式绑定版本、批量保存只 rebuild 一次、批量失败一个文件都不写、
rebuild 失败全部回滚）。

管理网页的测试全部在 `tmp_path` 的副本上跑，不会碰仓库里真实的 `data/`。

---

## Git 工作流

```text
新增 / 修改 YAML  →  validate_data.py  →  build_db.py  →  pytest  →  git commit
```

- **提交**：`data/` 下的 YAML、`sgs_helper/`、`scripts/`、`tests/`、`docs/`、README、pyproject 等。
- **不提交**：`build/`（含 `build/sgs.db`）、`__pycache__`、`.pytest_cache`、`*.egg-info`、虚拟环境。
- 每次改动 YAML 后请把 `build/sgs.db` 重新生成一遍再提交代码（数据库本身不入库）。

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
| `GET /api/skills/{id}/versions` | `skill_report(conn, id, as_of)["versions"]` |
| `GET /api/generals/{id}` | `general_report(conn, id)` |
| `GET /api/cards/{id}` | `card_report(conn, id)` |
| `GET /api/cards/{id}/printings` | `card_report(conn, id)["printings"]` |
| `GET /api/faq?q=伤害` | `faq_search(conn, q)` |
| `GET /api/keywords/{id}` | `keyword_report(conn, id)` |
| `GET /api/timings/{id}` | `timing_report(conn, id)` |

建议的推进顺序：① 把示例数据换成真实数据（最重要的价值来源）→ ② 用 `--json` 校验返回结构 →
③ 加 FastAPI（只读、无鉴权、单进程，读 SQLite 即可）→ ④ 再做基于资料库的 AI QA。

更多说明见 [docs/design.md](docs/design.md)（设计取舍与已知边界）、
[docs/admin.md](docs/admin.md)（本地资料管理网页：入口、写入流程、已知限制）、
[docs/operations.md](docs/operations.md)（维护手册）、[docs/roadmap.md](docs/roadmap.md)（路线图）。

## 已知边界（先不做，记录在案）

- 武将只有 `General` + `GeneralVariant` 两层：同一个武将的不同玩法/版本各写一条 `GeneralVariant`
  （各自带 `mode` / `hp` / `product_id` / `skills`），不需要「武将印刷」层。
- 技能文本没有结构化（没有 effects/targets/conditions 字段）：这是完整的规则引擎，超出当前阶段。
- 一个实体只带一个 `source`：需要多来源并列时，目前用 `verification_status: conflict` + `notes` 说明；
  以后可以扩展成来源关联表。
- 时点关系只记录先后，不做完整结算顺序模拟。
