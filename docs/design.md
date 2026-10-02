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

## 2. 为什么技能要拆两层

| | Skills（SkillDefinition） | SkillVersions |
| --- | --- | --- |
| 表示 | “集智”这个概念 | 某个模式/产品/时期下的具体文本 |
| 主键 | `skill_jizhi` | `skill_jizhi_identity_v2` |
| 变化 | 极少 | 每次平衡调整都新增 |

不拆层就会出现两种灾难：① 同一技能名在不同模式/产品下互相覆盖；
② 平衡调整后旧文本丢失，而玩家的实体卡上印的还是旧文本。

因此：

- 武将只引用 `skill_id`；
- 更新技能**只能新增** SkillVersion，并用 `previous_version_id` 串链；
- “当前有效版本”由 `mode + effective_from + effective_to` **推导**，不写 `current: true`。

### 当前有效版本的推导规则

```text
输入：skill_id、请求模式 M、基准日期 as_of（默认今天）
1. 候选 = 版本中 skill_id 相同 且 mode ∈ {M, common} 且 区间包含 as_of 的
2. 生效区间按日期闭区间：effective_from ≤ as_of ≤ effective_to
   （effective_from 为空视为 -∞，effective_to 为空视为 +∞）
3. 具体模式优先于 common（MODE_SPECIFICITY）
4. 取 effective_from 最晚者
5. 若仍并列 → 判定为“有歧义”，全部列出并提示，绝不静默挑一个
```

边界情况与处理：

| 情况 | 处理 |
| --- | --- |
| 两个版本同日生效、区间重叠 | 由校验器告警（同一 skill_id + mode + 产品范围） |
| `common` 版本与具体模式版本同时生效 | 具体模式版本胜出（并显示“来自 common”） |
| 版本被“撤销”但区间没改 | 只能靠新增一条版本并收紧旧版本 `effective_to`；校验器会告警重叠 |
| 只有未来版本 | 该模式当前无有效版本，查询显示“尚未生效” |
| 产品特例与通用版本并存 | 目前以 `mode` 优先；产品级优先级留待有真实需求时再加 `scope` |

## 3. 为什么卡牌要拆两层

- `CardDefinition` 回答“杀是什么”（规则意义）；
- `CardPrinting` 回答“我手里这张杀是哪一盒里的、什么花色点数、角标是什么、卡面怎么印的”。

同一张 `杀` 在不同产品里的 `printed_text` 可能不同，**必须分开存**；
`product_id` 是必填的，否则无法回答“这张卡来自哪个产品”，也就无法排查“牌堆里是否混入了其他产品的卡”。

`bottom_right_mark` 单列（不进 notes），因为它是**结构性信息**：
以后要查“所有带君角标的卡”“某角标对应哪些产品”，都必须能直接筛选。

## 4. 可追溯性做到什么程度

| 层 | 记录内容 | 用途 |
| --- | --- | --- |
| YAML | 每条记录内嵌 `source`（类型/标题/引用/备注） | 人工判断依据 |
| 装载 | 每个实体记录来源文件、起始行、文件 sha256 | 报错精确定位；判断是否改动过 |
| 数据库 | 每行带 `source_key` / `yaml_file` / `yaml_line`，`sources` 表去重 | 任何一条查询结果都能回溯到文件与行 |
| 构建 | `meta` 表记录 schema 版本、构建时间、YAML 清单指纹、Git commit/branch/dirty | 判断数据库是否过期、对应哪次提交 |

刻意**没做**的：内容级哈希、逐字段血缘、来源多对多关联表。数据量小，上面这些已经够查。

## 5. SQLite 结构与检索

- 一种实体一张表；多值字段（`skill_ids`、`related_keywords` 等）以 JSON 文本存储，
  另外把武将↔技能额外落一张 `general_skills` 关联表，方便以后做反向查询。
- 建了必要的索引：`skill_versions(skill_id, mode)`、`skill_versions(skill_id, effective_from, effective_to)`、
  `card_printings(card_id)`、`card_printings(product_id)`、`card_printings(bottom_right_mark)` 等。
- 统一的 `search_index(kind, entity_id, name, aliases, body)` 承担检索：
  `body` 在构建时拼好（技能版本正文、FAQ 问答、关联对象名称等），查询层不需要复杂 SQL。
- 检索顺序：精确名称 → 别名精确 → 名称前缀 → 名称包含 → 正文包含；数据量很小，够用且结果可解释。
- FTS5：可用时建 `fts_search` 做兜底。**中文处理**：FTS5 的 `unicode61` 会把一整串汉字当成一个 token，
  所以写入时把 CJK 字符逐字加空格，查询时把汉字序列转成短语（`"集 智"`），
  这样“集智”也能命中“集智测试”。没有 FTS5 的环境自动回退 `LIKE`。
- 刻意没做：`entities` 通用视图、跨类型 id 唯一性检查、语义/向量检索、独立检索抽象层。

## 6. 校验器检查清单

**错误（必须修）**

1. YAML 语法、顶层结构、条目必须是映射；
2. 必填字段缺失、未知字段、枚举取值非法、id 格式非法、区间起止倒置、`release_year` 与 `release_date` 年份不一致；
3. 同类型内 **id 重复**（报出两处文件与行号）；
4. 所有引用不存在（并提示“该 id 属于另一种类型，是否字段写错”）；
5. 空引用、自引用、`previous_version_id` 属于别的技能、版本链成环、时点 parent 链成环。

**警告（不影响建库，但应当关注）**

1. 同一技能同一模式同一产品范围生效区间重叠或完全相同（会导致“当前有效版本”歧义）；
2. `previous_version_id` 的 `effective_from` 晚于当前版本（版本顺序可疑）；
3. 同一产品同一牌堆出现相同花色点数的多张实体卡；
4. 武将/实体卡的 `mode` 与所属产品的 `mode` 不一致；
5. 武将引用的技能在该模式下没有任何 SkillVersion（查询时拿不到文本）；
6. 技能本体没有任何 SkillVersion；
7. 有花色却没点数（或反之）；
8. 体力不是 0.5 的整数倍；
9. FAQ 的 `mode` 与关联武将的 `mode` 不一致；
10. 时点 `next_ids` / `previous_ids` 未双向一致；
11. 列表内重复引用；产品/武将/技能本体缺 `source`。

## 7. 明确不做（当前阶段）

- 微信登录、用户系统、管理后台、云数据库、Docker/K8s、微服务；
  （本地的 `sgs_helper/admin/` 资料维护网页不算在这个清单里：它只在本机跑、
  没有登录与多用户，直接写 YAML 权威源，见 [admin.md](admin.md)）
- Elasticsearch、复杂 RAG、大模型 Agent、自动抓取全网；
- 完整规则模拟器、技能效果结构化（effects/targets/conditions）。

## 8. 已知边界与将来的扩展点

| 边界 | 现状 | 将来的做法 |
| --- | --- | --- |
| 同名武将跨产品 | 用不同的 `general.id`（各自带 `product_id`），必要时加 `aliases` | 若成为常态，再抽 `GeneralPrinting` 层 |
| 一个实体多来源 | 单 `source` 字段 + `conflict` 状态 + notes 说明 | 抽 `entity_sources` 关联表 |
| 产品级规则特例 | `mode` 优先，未做产品级优先级 | 给 SkillVersion 加 `scope` / `rule_priority` |
| 时点结算顺序 | 只记录 prev/next/parent | 需要时再引入顺序号与嵌套时点 |
| 技能效果检索 | 全文检索规则文本 | 需要时再加结构化字段（不与文本混用） |
| 表结构变更 | `SCHEMA_VERSION` + 每次全量重建 | 数据量变大后再考虑迁移脚本 |
