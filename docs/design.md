# 设计说明

本文记录**为什么这样设计**，以及哪些东西故意没做。所有取舍的第一优先级是：
**简单、正确、可追溯、可长期维护**，其次才是性能与扩展性。

## 1. 数据流

```text
data/**/*.yaml  （人工维护，Git 唯一权威源）
        │  sgs_helper/loader.py        装载 + 记录 文件/行号/文件哈希
        ↓
   Pydantic 模型  sgs_helper/schema.py   字段级校验（未知字段直接报错）
        │  sgs_helper/validate.py      交叉引用 + 一致性检查
        ↓
   DataBundle（内存中的完整数据集）
        │  sgs_helper/db.py           全量重建（删旧库→建表→写入→写 meta）
        ↓
build/sgs.db  （生成物，随时可重建，不入 Git）
        │  sgs_helper/repo.py         只读查询，返回纯 dict
        ↓
sgs_helper/cli.py → 人可读文本 / JSON     （以后：FastAPI → 微信小程序）
```

关键点：**校验与构建是分开的两次执行**（`validate_data.py` / `build_db.py`），
`build_db.py` 内部也会先校验一遍，校验不过就退出（返回码 2），避免生成半成品数据库。

数据来源策略：规则侧数据分两批 —— 技能版本来自
[《三国杀官方规则集 3.0》的网页版](https://gltjk.com/sanguosha/rules/)，
FAQ 与规则关键词来自《群狼环鼎规则资料包 v1.0》的人工核对稿（规则手册 19 页照片 + 实卡照片 + 通用规则网页 + B 站 UP 主文章）；实体卡数据来自
群狼环鼎卡面照片；用 `source.type`（`official_rule` / `physical_card` …）记录“内容是什么”，
不因为文档名称里带“官方”就默认可信。
实体卡类实体（Product / GeneralVariant / Card）不存 `source`，来源由 `product_id` / `card_set` 定位。

解析性能：数据长到近千个 YAML 之后，`loader` 做了两件事 ——
有 libyaml 就用 C 解析器（快 5～7 倍），以及单条映射不再为了拿行号多解析一遍；
`AdminStore` 则对“文件指纹”做了毫秒级缓存，避免渲染一页时把上千个文件 stat 上万次。

### 1.1 本地管理网页的写入路径（`sgs_helper/admin/`，可选）

浏览器维护资料的路径与上面完全相同，只是把「人手工编辑 YAML」换成「表单写 YAML」：

```text
浏览器表单（sgs_helper/admin/forms.py 由模型生成）
        │  先在内存里合成候选实体
        ↓
validate.validate_entities(raws, …)     与 CLI 同一套校验（模型 + 交叉引用 + 一致性）
        │  不通过 → 一个字都不落盘，页面显示 文件/字段/原因
        ↓
yaml_writer（临时文件 + fsync + os.replace）   字段顺序按模型、长文本用块状标量、原子替换
        ↓
data/**/*.yaml
        │  build_database()：先写临时库，成功后 os.replace 到 build/sgs.db
        ↓
build/sgs.db（网页不会直接改它；任何一处 UPDATE/INSERT 都不存在）
```

两个刻意的取舍：

1. **读也走 YAML**。页面渲染、列表、搜索都直接读权威源（`validate_data_dir` 得到的 bundle +
   `db._search_row` + `repo._score`），而不是读可能过期的 `build/sgs.db`，
   这样“看到的内容”和“编辑的内容”永远是同一份；SQLite 只服务于 CLI 与未来的小程序。
2. **写盘前后各校验一次**。写盘前用内存候选快照校验（保证磁盘上不出现非法 YAML），
   写盘后由 `build_database` 从磁盘重新装载再校验一次（保证建库用的是磁盘真值）。
   编辑页还会带上该记录的内容哈希，页面打开后文件被改动过就拒绝写入，避免覆盖别人的修改。

### 1.2 录入模型的三处调整（为了“拿着卡从上往下录”）

1. **产品里“武将牌系列”和“游戏牌拓展包”分开**：
   旧字段 `included_sets` 把两者混在一个列表里，录卡时没人分得清哪个是哪个。
   现在拆成 `Product.general_sets` 与 `Product.card_sets`；
   前者是武将录入页「武将牌系列」的候选，后者是卡牌录入页「所属拓展包」的候选。
2. **武将 → 技能改成关系结构**：
   旧的 `skill_ids: [...]` 只能回答“有哪些技能”，回答不了“这张卡印的是哪一版”。
   现在是 `skills: [{skill_id, version_id}]`：`version_id` 填了就按它取文本，
   为空则按该武将版本的 `mode` 匹配该技能唯一一条版本；
   不为空时必须属于同一个 `skill_id`（校验器强制，SQLite 落到
   `variant_skills(variant_id, general_id, skill_id, version_id, position)`）。
   不用两个平行数组，避免顺序错位。
3. **内部 id 与卡面编号分开存，但 id 由编号派生**：
   `id` 做内部主键（YAML 文件名 / URL / 外键），卡面编号放
   `GeneralVariant.general_number`（自由字符串，**与卡面完全一致**：大小写、点、空格、`&` 都保留）。
   `id` 是编号的规范化形式（`general_` + 编号小写、非字母数字换下划线），
   所以编号一改，`General.id` / `GeneralVariant.id` / 卡图文件名 / 引用它的 `general_id`
   会一起跟着改——批量 OCR 录入后按卡图核验修正编号是常规操作，不能要求手工同步四份。
   编号本身不做字符替换，保证前端展示与卡面一致。

另外，武将的 `hp`（勾玉）与 `mode` 允许为空：**只有名字必填**。
这是刻意的取舍 —— 资料库要允许“先录手头有的信息，以后再补全”，
缺东西最多产生 warning，不能因为缺一个字段就整条存不下来。

### 1.3 一次提交 = 一个事务

武将个体与玩法版本分成两层：`General`（id / name）与
`GeneralVariant`（模式 / 势力 / 体力 / 性别 / 称号 / 编号 / 产品 / 武将牌系列 / 技能）。
官方名不同 → 不同 General；官方名相同、玩法不同 → 同一 General 的不同 Variant。
武将录入页一次提交可能产生 `1 个 General + 1 个 GeneralVariant + N 个 Skill + N 个 SkillVersion`。
如果按条调用保存，就会多次重建数据库、中途失败还会留下半套数据。
所以写入口收敛到 `AdminStore.save_batch()`：合并内存候选 → 一次完整校验 →
一次写盘（逐个原子替换）→ **一次 rebuild**；失败则全部回滚。

## 2. 为什么技能要拆两层

| | Skills（SkillDefinition） | SkillVersions |
| --- | --- | --- |
| 表示 | “集智”这个概念 | 某个模式/产品下的具体文本 |
| 主键 | `skill_ji_zhi` | `skill_ji_zhi_guozhan_qlhd_v1` |
| 变化 | 极少 | 每次平衡调整都新增 |

不拆层就会出现两种灾难：① 同一技能名在不同模式/产品下互相覆盖；
② 平衡调整后旧文本丢失，而玩家的实体卡上印的还是旧文本。

因此：

- 武将版本用 `skills[].skill_id` 引用技能本体；
- 更新技能**只能新增** SkillVersion，并用 `previous_version_id` 串链；
- 用哪一版文本由 `GeneralVariant.skills[].version_id` **显式绑定**，不按日期推导，
  也不写 `current: true`。

### 取哪一版技能文本

```text
输入：武将版本 V、技能 skill_id
1. V.skills[].version_id 填了就按它取文本
   （校验器保证它属于同一个 skill_id）
2. 为空时，取该技能下 mode ∈ {V.mode, common} 的版本
3. 命中唯一一条 → 用它；0 条或多条并列 → 判为“这个技能在此武将版本上没有文本”
```

边界情况与处理：

| 情况 | 处理 |
| --- | --- |
| 显式绑定的版本属于别的技能 | 校验器报错（error），必须先修 |
| 同技能同模式有多条版本、又没显式绑定 | 无法确定：查询显示“没有文本”，提示补 `version_id` |
| 只有 `common` 版本 | 具体模式没有版本时回退到它 |
| 产品特例与通用版本并存 | 目前以显式 `version_id` 优先；产品级规则留待有真实需求时再加 `scope` |

## 3. 为什么卡牌只留一层

游戏牌最终收敛成**一层** `Card`：

- 三级分类（`category_1/2/3`）回答“这张卡是什么”（基本牌 / 锦囊牌 / 装备牌…）；
- `name` 回答“具体是哪一张”（本项目的习惯是基本牌的 `category_3` 写牌族 杀 / 闪 / 桃 / 酒，
  具体牌名写在这里；分类本身自由填写、二 / 三级可留空，校验器不强制）；
- `card_set` 回答“它来自哪个拓展包”（与分类完全独立的一个维度，允许为空）；
- `effect: {text, skills}` 回答“卡面写了什么、关联了哪些技能”。

原来的 `CardPrinting` 层（某产品实际印刷的一张实体卡：花色 / 点数 / 角标 / 卡面文本）**已整体删除**：
个人资料库当前只需要卡面资料，同一规则卡的花色 / 点数印刷属于“完整牌堆构成”，
留给以后单独加的 `deck_entries`，不再为了花色点数把同一张卡拆成多条。

`card_set` 不再是产品外键：卡与产品**没有外键**，唯一关系是
`Card.card_set ∈ Product.card_sets`（同名拓展包可被多个产品共用，产品页会重复计数）。
「指示物」是卡牌分类（`category_1`），不是拓展包。

## 4. 可追溯性做到什么程度

| 层 | 记录内容 | 用途 |
| --- | --- | --- |
| YAML | SkillVersion 内嵌 `source`（只有 type + reference）；FAQ 的 `source` 是多行文本 | 人工判断依据 |
| 装载 | 每个实体记录来源文件、起始行、文件 sha256 | 报错精确定位；判断是否改动过 |
| 数据库 | 每行带 `yaml_file` / `yaml_line`；只有 `skill_versions` 另带 `source_key`（FAQ 的 source 已是文本列、关键词无来源），`sources` 表去重 | 任何一条查询结果都能回溯到文件与行 |
| 构建 | `meta` 表记录 schema 版本、构建时间、YAML 清单指纹、Git commit/branch/dirty | 判断数据库是否过期、对应哪次提交 |

刻意**没做**的：内容级哈希、逐字段血缘、来源多对多关联表。数据量小，上面这些已经够查。

## 5. SQLite 结构与检索

- 一种实体一张表；多值字段（`general_sets`、`card_sets`、`zhulianbihe`）以 JSON 文本存储，
  另外把「武将版本 ↔ 技能」额外落一张 `variant_skills` 关联表，方便以后做反向查询。
- 建了必要的索引：`skill_versions(skill_id, mode)`、`skill_versions(previous_version_id)`、
  `cards(category_1, category_2)`、`cards(card_set)`、
  `general_variants(general_id)` / `general_variants(mode)` / `general_variants(product_id)` 等。
- 统一的 `search_index(kind, entity_id, name, body)` 承担检索：
  `body` 在构建时拼好（技能版本正文、FAQ 问答、关联对象名称等），
  查询层不需要复杂 SQL。关联对象**不再手工维护**：构建时按实体名称在正文里自动识别，
  把命中的名字拼进 `body`，详情页也用同一套识别生成可点击链接。
- 检索顺序：精确名称 → 名称前缀 → 名称包含 → 正文包含；数据量很小，够用且结果可解释。
- FTS5：可用时建 `fts_search` 做兜底。**中文处理**：FTS5 的 `unicode61` 会把一整串汉字当成一个 token，
  所以写入时把 CJK 字符逐字加空格，查询时把汉字序列转成短语（`"集 智"`），
  这样“集智”也能命中“集智测试”。没有 FTS5 的环境自动回退 `LIKE`。
- 刻意没做：`entities` 通用视图、跨类型 id 唯一性检查、语义/向量检索、独立检索抽象层。

## 6. 校验器检查清单

**错误（必须修）**

1. YAML 语法、顶层结构、条目必须是映射；
2. 必填字段缺失、未知字段、枚举取值非法、id 格式非法、`source` 缺 `type`；
3. 同类型内 **id 重复**（报出两处文件与行号）；
4. 所有引用不存在（并提示“该 id 属于另一种类型，是否字段写错”）；
5. 空引用、自引用、`previous_version_id` 属于别的技能、版本链成环、
   `skills[].version_id` 属于别的技能。

**警告（不影响建库，但应当关注）**

1. 归属对不上：武将 `mode` 与所属产品的 `mode` 不一致、
   `GeneralVariant.general_set` 不在所属产品的 `general_sets` 里、
   `Card.card_set` 没有被任何产品的 `card_sets` 声明（写成「指示物」会给专门提示）；
2. 基本牌的 `category_3` 不是牌族（杀 / 闪 / 桃 / 酒）；
3. `Card.image` / `GeneralVariant.image` 指向的卡图文件不存在；
4. 武将引用的技能在该模式下没有任何 SkillVersion，且 `version_id` 为空（查询时拿不到文本）；
5. 技能本体没有任何 SkillVersion；
6. 体力不是 0.5 的整数倍；
7. 武将没有任何版本；同一武将出现重复的（`variant` + `mode`）；
8. 列表内重复引用（同一个武将版本重复引用同一个技能、同一张卡重复引用同一个技能）。

## 7. 明确不做（当前阶段）

- 微信登录、用户系统、管理后台、云数据库、Docker/K8s、微服务；
  （本地的 `sgs_helper/admin/` 资料维护网页不算在这个清单里：它只在本机跑、
  没有登录与多用户，直接写 YAML 权威源，见 [admin.md](admin.md)）
- Elasticsearch、复杂 RAG、大模型 Agent、自动抓取全网；
- 完整规则模拟器、技能效果结构化（effects/targets/conditions）。

## 8. 已知边界与将来的扩展点

| 边界 | 现状 | 将来的做法 |
| --- | --- | --- |
| 同名武将跨产品/玩法 | 同一个 `general.id`，每个玩法各写一条 `GeneralVariant`（各自带 `mode` / `product_id` / `skills`） | 不需要「武将印刷」层 |
| 一个实体多来源 | SkillVersion 带单条 `source`（type + reference），FAQ 的来源是多行文本，关键词不记来源；实体卡类实体由 `product_id` / `card_set` 定位、不记 source | 抽 `entity_sources` 关联表 |
| 产品级规则特例 | 以显式 `version_id` 优先，未做产品级优先级 | 给 SkillVersion 加 `scope` / `rule_priority` |
| 技能效果检索 | 全文检索规则文本 | 需要时再加结构化字段（不与文本混用） |
| 表结构变更 | `SCHEMA_VERSION` + 每次全量重建 | 数据量变大后再考虑迁移脚本 |
