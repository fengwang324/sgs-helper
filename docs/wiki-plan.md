# 本地 Wiki 方案（Phase 1：现状分析与最小改造方案）

> 目标：把现有「资料库底座 + 本地管理页」推进成一个**能日常使用的本地 Wiki**：
> 浏览资料、验证前端交互、并且能直接在网页上增删改资料。
>
> Wiki 本身**不是**最终线上服务，而是 **资料浏览器 + 本地编辑器 + UI 原型**。
> 数据始终以 `data/` 下的 YAML 为唯一权威源。

---

## 0. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 用什么技术栈 | **扩展现有 Python `sgs_helper/admin/`**（FastAPI + Jinja2），不新建 Node/TS monorepo |
| 数据模型要改多少 | **极少**：`CARD_CATEGORY_TREE` 补 `指示物` 子树；其余**不改模型** |
| FAQ 关联怎么做 | **沿用现有的「按名称自动识别」**，新增**按 id 的反向索引**；不引入手工 `related` 字段 |
| 新页面 | `/wiki/*` 五个一级入口，复用同一进程、同一端口 |
| 视觉 | 新建 `static/wiki.css`，对齐 open-shap2-web 的 token 与版式；**不改动**现有 `style.css` |

**为什么不做 Node/TS monorepo**：见第 5 节。核心原因是——现有 admin 已经实现了
整套「安全写 YAML」能力（内存全量预校验 → 原子写 → 回滚 → 记录级并发保护 → CSRF → 一次 rebuild），
并有 95 条测试盯着这条写入路径；换语言重写等于把这块最大资产清零，
而未来微信小程序（WXML）**并不共享 TS**，真正要共享的是 `repo.py` 之上的只读 API。

---

## 1. Phase 1：现状分析

### 1.1 数据现状（实测，`python scripts/validate_data.py`）

**1247 条实体 / 0 错误 / 14 警告**（14 条全是「技能本体没有任何 SkillVersion」）。

| kind | 目录 | 条数 | 说明 |
| --- | --- | --- | --- |
| product | `data/products/` | 1 | 只有 `qlhd`（群狼环鼎） |
| general | `data/generals/` | 155 | 武将个体，只有 `id` / `name` |
| general_variant | `data/general_variants/` | 163 | 势力/体力/称号/编号/技能/卡图 |
| skill | `data/skills/` | 311 | 技能本体 |
| skill_version | `data/skill_versions/` | 297 | 技能版本文本（全部国战） |
| card | `data/cards/` | 81 | **全部 `category_1: 游戏牌`** |
| faq | `data/faq/` | 85 | 群狼环鼎规则资料包核对稿 |
| keyword | `data/keywords/` | 117 | 规则关键词 |

卡图：`data/card_images/` 356 个文件。

### 1.2 分类结构现状

`constants.CARD_CATEGORY_TREE` **只有游戏牌一棵树**：

```python
CARD_CATEGORY_TREE = {
    "游戏牌": {
        "基本牌": ["杀", "闪", "桃", "酒"],
        "锦囊牌": ["非延时锦囊", "延时锦囊"],
        "装备牌": ["武器", "防具", "坐骑", "宝物"],
    },
}
```

`Card.category_1/2/3` 在模型里是**自由字符串**（`Name`），不做枚举校验。

### 1.3 图片现状

- `Card.image` / `GeneralVariant.image` 存的是**文件名**，语义上是「相对 `data/card_images/`」。
- `validate.py` 会检查图片文件是否存在。
- admin 通过 `/card-images` 静态挂载展示。
- **缺口**：`GeneralVariant.image` 模型里有字段，但**当前没有任何页面渲染或编辑它**。

### 1.4 编辑能力现状（已有，且很强）

`GET/POST /new|/edit/{kind}` 已有：

- 专用录入页：产品 / 武将 / 卡牌（字段顺序按「拿卡从上往下抄」设计）；
- generic 表单：技能本体 / 技能版本 / FAQ / 关键词（由 Pydantic 模型生成）；
- 删除确认 + 引用检查（被引用则拒绝并列出引用方）；
- 写入安全：内存合入全量数据集 → 完整校验 → 不通过一个字都不落盘 →
  临时文件 + fsync + `os.replace` 原子落盘 → 只 rebuild 一次 SQLite；
- 并发保护：内容哈希 + CSRF token，两个标签页同时改会被拒。

**结论：Phase 4「编辑」绝大部分已经存在，Wiki 要做的是「换个入口 + 补缺口」，不是从零写 CRUD。**

### 1.5 schema / validation 现状

- `schema.py`：9 个 Pydantic 模型，全部 `extra="forbid"`（写错字段名直接报错）。
- `validate.py`：718 行，覆盖引用存在性、版本链成环、体力步进、模式一致性、
  基本牌三级分类是否为牌族、`card_set` 是否被产品声明、卡图文件是否存在……
- `db.py`：YAML → SQLite 全量重建，FTS5 中文逐字分词，`search_index.body` 里
  **已经包含「按名称自动识别出来的关联实体名」**。

### 1.6 关联（relation）现状 —— 关键发现

**没有手工关联字段。** 关联一律由**实体名称在正文中出现**自动识别：

- 建库：`db.build_name_index()` / `db.detect_mentions()` → 写进 `search_index.body`；
- 查询：`repo.mentions(conn, text, kinds, limit)`（最长名称优先，名称长度 < 2 不参与）；
- 页面：`store.mentions()` / `store.annotate()` 把正文切成「普通文本 / 可点击实体」片段。

这是**刻意的设计**（README 设计原则 5、`schema.py` 模块 docstring），
`related_*` 手工字段是被删掉的。

**两个必须处理的现实问题：**

1. **反向（实体 → FAQ）查询不存在。** `repo.py` 只有「给一段文本找实体」，
   没有「给一个实体找提到它的 FAQ」。而需求里
   「武将/技能/卡牌详情页显示相关 FAQ」正是反向查询。
2. **规则类 FAQ 极其庞大。** 例如 `faq_rule_flow_damage.yaml` 的 `answer` 有数千字，
   罗列了上百个技能名。如果反向索引直接「命中就算相关」，
   那么**几乎每一个技能都会挂上一堆规则专题 FAQ**，关联区会被噪声淹没。

   → 所以反向索引必须带**相关度**判断，不能只做「命中/未命中」二值。

### 1.7 验收细则对应的现状差距

| 验收项 | 现状 | 差距 |
| --- | --- | --- |
| 1 武将/技能/游戏牌可浏览 | admin 已有 list/detail | 需要 Wiki 版版式与入口 |
| 2 指示物空状态正常 | — | 需要新页面 + 空状态 |
| 3 网页新增指示物并落盘 | `/new/card` 可用，但分类候选里没有指示物 | **需要补 `CARD_CATEGORY_TREE`** |
| 4 网页新增「测试锦囊」 | 可做 | 需要分类树可导航 |
| 5 新增 FAQ 并关联武将+技能 | 可新增 FAQ；关联**自动识别** | 需要反向索引 + 详情页区块 |
| 6 改 FAQ 后各位置同步 | 自动识别天然满足 | 需要验证 |
| 7 编辑后真实写回 data | 已满足 | 需要验证 |
| 8 图片上传 + 相对路径 | **没有上传接口** | **需要新增** |
| 9 validation 通过 | 已满足 | — |
| 10 type-check / build 通过 | 无 TS；对应 `validate_data.py` + `pytest` | 需要跑通 |

---

## 2. 目标结构（务实版）

用户建议的 `apps/` + `packages/` monorepo 与现有单包结构冲突。**采用最小改造**：
保留 `sgs_helper` 单包，Wiki 作为其中的新页面组。

```text
sgs-helper/
├── data/                        # ★ 唯一权威数据源（不动）
├── sgs_helper/                  # ★ 唯一实现（不动结构）
│   ├── constants.py             # ← 补 指示物 分类子树
│   ├── schema.py                # 不改
│   ├── validate.py              # ← 补 指示物 分类一致性告警
│   ├── db.py / repo.py / ...    # 不改
│   └── admin/                   # ← Wiki 就建在这里
│       ├── app.py               # ← 新增：挂载 /wiki 路由
│       ├── wiki.py              # ← 新增：Wiki 页面逻辑
│       ├── store.py             # ← 新增 wiki_nav_tree / related_faqs / filter_options_scoped
│       ├── templates/wiki/      # ← 新增：Wiki 模板
│       └── static/wiki.css      # ← 新增：Wiki 样式（不动 style.css）
├── scripts/                     # validate_data.py / build_db.py / query.py / admin.py
├── tests/                       # ← 新增 test_wiki.py
└── docs/
    ├── wiki-plan.md             # ← 本文件
    └── wiki.md                  # ← 使用说明
```

「apps/wiki」「apps/dev-server」的职责边界在本方案里是：

- **apps/wiki（页面层）** = `admin/templates/wiki/` + `admin/static/wiki.css` + `admin/wiki.py`
- **apps/dev-server（数据服务）** = `admin/app.py` + `admin/store.py`（复用现有读写与校验）
- **packages/core（公共逻辑）** = `constants.py` / `schema.py` / `db.py` / `repo.py` / `validate.py`（已存在，不复制）

即：**职责划分照搬用户的设计，但不物理搬目录、不换语言。**

---

## 3. 数据模型调整（最小集）

### 3.1 必改

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `sgs_helper/constants.py` | `CARD_CATEGORY_TREE` 增加 `指示物` 子树：`军令 / 鏖战事件 / 技能指示物 / 衍生技能牌 / 通用标记` |
| 2 | `sgs_helper/validate.py` | 新增告警：`category_1 == 指示物` 但 `category_2` 不在该子树内 |

第 1 条的后果检查：`validate.py` 的 `BASIC_CARD_FAMILIES` 从「游戏牌 → 基本牌」派生，
**不受影响**；`Card.card_set` 校验与 `Product.card_sets` **不受影响**（指示物是分类，不是拓展包）。

### 3.2 「从网页新建二级/三级分类」怎么满足（不改模型）

卡牌录入页的分类候选本来就是「`CARD_CATEGORY_TREE` ∪ 现有卡牌实际取值」，
并且**允许直接输入新值**（`admin/editors.py`）。

于是：**用户在 `/new/card` 里输入一个新分类名并保存 = 新建了这个分类。**
Wiki 侧的分类树渲染为「静态树 ∪ 数据实际取值」，新分类下一次渲染立刻出现。

代价：**没有任何卡引用过的分类不会预先显示**。这对指示物无影响——
指示物 5 个二级分类来自 3.1 的静态子树，所以空状态页能正常列出它们。
若以后确实需要「预建空分类」，再引入**单个** `data/card_category_tree.yaml`
（不注册成 kind，只被 loader 读一个辅助函数），Phase 1 不做。

### 3.3 明确**不改**的东西

- ❌ **不给 `Faq` 加 `related` / `related_*` 字段。**
  自动识别已深植于 `db._search_row` → `search_index.body` → `repo.mentions` → `store.annotate`。
  加手工字段 = 与现有机制打架，且是设计上的倒退。
  真正缺的只是**反向索引**，那是查询层的事，不是模型的事。
- ❌ 不新增实体 kind 来放分类树（会牵动 `KINDS` / 建库 / 校验全链路）。
- ❌ 不动 `Product.card_sets`（「指示物」是 `category_1`，README 已明确）。
- ❌ 不 bump `SCHEMA_VERSION`（Phase 1 不改 SQLite 表结构）。

---

## 4. 实现设计

### 4.1 五个一级入口

| 路由 | 数据 | 说明 |
| --- | --- | --- |
| `/wiki` | — | 首页：标题 + 五个 tab + 搜索框 + 卡片列表 |
| `/wiki/generals` | `general`（含 variant 摘要） | 武将 |
| `/wiki/skills` | `skill` | 技能 |
| `/wiki/cards` | `card` where `category_1 == 游戏牌` | 游戏牌 |
| `/wiki/markers` | `card` where `category_1 == 指示物` | 指示物（当前 0 条 → 空状态） |
| `/wiki/faq` | `faq` | FAQ |
| `/wiki/rules` | — | **预留占位**（Phase 1 不做内容） |
| `/wiki/search` | 全部 | 全库搜索（跨四类，按类型分组） |

统一支持 `?q=` 搜索与 `?cat2=&cat3=` 分类筛选，`?page=` 分页（每页 48 条）。

### 4.2 详情页与关联跳转

| 详情页 | 展示 | 关联出口 |
| --- | --- | --- |
| 武将 | 卡面、称号、名字、势力、体力、性别、模式、编号、武将牌系列、产品、技能、变体 | 技能（→技能详情）、相关 FAQ |
| 技能 | 技能名、各版本描述、所属武将 | 武将（→武将详情）、相关 FAQ |
| 卡牌 | 卡图、三级分类、拓展包、效果文本、关联技能 | 技能、相关 FAQ |
| 指示物 | 同卡牌（靠 `category_1` 区分） | 同上 |
| FAQ | 问题、答案（正文内实体名可点击）、版本标签、来源（多行文本） | 相关武将 / 技能 / 卡牌 / 关键词 |

**新增的反向索引**：`store.related_faqs(kind, entity_id)`。

实现要点（解决 1.6 的噪声问题）：

```text
对每条 FAQ：
  对 question 与 answer 分别做 mentions() → 得到命中的实体集合
  相关度 = f(命中位置)
    - 在 question 里          → 100（用户问的就是它）
    - 在 answer 开头 200 字内  → 50
    - 在 answer 前半段         → 20
    - 只在超长正文的后半段     → 不计
  只保留 >= 20 分的，并按相关度排序、限量（详情页取前 6 条）
```

**一律按 `(kind, id)` 比对，不按名称比对**（避免同名误关联）。

**性能**：反向索引按文件指纹缓存（`store._faq_index`）；85 条 FAQ 建一次约 0.2 秒，
之后 0 成本，不会随每次请求重复扫描。

### 4.3 需求「新增 FAQ 并关联武将和技能」如何满足

因为关联是**按名称自动识别**的，所以只要 FAQ 正文里出现武将名 / 技能名即自动关联。
验收时：新增一条 FAQ，正文里写上某个真实武将名与技能名 → 该武将/技能详情页
即出现该 FAQ，FAQ 详情页也出现可点击的武将/技能链接。

> 注意：实体名长度 < 2 不参与识别（避免「杀」把全文刷成链接）。
> FAQ 详情页会**明确标注**「关联为按名称自动识别，可能误报」。

### 4.4 编辑模式

- 默认**浏览态**。
- 顶部一个「编辑模式」开关 → 写 cookie `sgs_edit=1`，服务端在 `_ctx()` 里读。
- 开启后：列表页出现「+新增」；详情页出现「编辑 / 复制 / 删除」。
- 零 JS 也能工作（表单提交后 303 回原页）；`wiki-upload.js` 只负责上传与开关。
- 「复制」复用现有 `?from=<id>` 模板新增能力。

### 4.5 图片上传

新增 `POST /api/upload-image`：

- `os.path.basename` 去掉路径；
- 扩展名白名单 `{jpg, jpeg, png, webp}`，其余直接拒绝；
- 单张大小上限 8MB；
- 重名自动加后缀；
- 写入 `data/card_images/`，**返回文件名**（与 `Card.image` 语义一致）；
- 同时补上**武将卡图字段的渲染与编辑**。

风险：`validate.py` 会检查图片存在性 —— 上传成功的文件必然存在，所以通过；
白名单与校验口径已对齐（都是 jpg/jpeg/png/webp）。

### 4.6 视觉规范（对齐 open-shap2-web）

从参考项目 `.skills/ui-preferences/preferences.md` 与 `WikiView.vue` 提取：

```css
--page-p: 28px;        --page-inset: 20px;
--page-gap: 16px;      --page-r: 12px;
/* 内容区 max-width: 1080px，居中；白底、黑主文字、浅灰边框 */
```

硬规则（参考项目明确禁止的）：

- **panel 只有一层 border**；内层 inset **只给底色 + 圆角，不再套 border**；
- 禁止 panel 套 card 套 inset（盒中盒）；
- 大区域**禁用主题色 wash 铺底**；
- 内容页标题用 `page-head-compact`（H1 24px/700），**不用** hero 级大标题；
- H2 用 19px/800 + 左侧 5px 竖条；
- 正文 14px / `line-height: 1.75`；
- 卡片圆角 12px。

---

## 5. 为什么不采用 Node/TS monorepo（决策记录）

用户建议 `apps/wiki`（uni-app + Vue3 + TS）、`apps/dev-server`（Node + TS）、`packages/core`。
**经评估不采用**，理由：

1. **写入安全网会整段消失。** 今天 `AdminStore.save_batch()` 保证「校验不通过一个字都不落盘」。
   Node 侧重写这套要数周，或退化成「先写 YAML 再校验」——**磁盘上会出现半合法 YAML**，
   而这正是已经花力气堵掉的洞。
2. **跨语言重建。** YAML 写完必须重建 SQLite，而 `build_database()` 在 Python 里。
   Node dev-server 要么 `child_process` 调 Python（白拿一个「必须装 Python 的 Node 服务」），
   要么在 TS 里重写建库器（第二套 schema + FTS5 中文分词规则）。
3. **`extra="forbid"` 护栏在 TS 侧不存在。** YAML 写错字段名现在直接报错；
   TS 侧除非用 zod 重建等价严格性，否则会**静默丢字段**、慢性污染权威数据。
4. **uni-app H5 打不到参考项目的观感。** 参考项目的观感来自 Naive UI + token 体系，
   而 uni-app 有自己一套组件与运行时，**不能直接用 Naive UI**。
5. **「共享 TS 给小程序」收益是假的。** 小程序是 WXML + JS；
   `docs/roadmap.md` 阶段 2/3 已定：小程序 → 只读 FastAPI → SQLite。
   真正要共享的是 `repo.py`（已是纯 dict 返回），不是类型定义。
6. **「最小改造方案」与「重写编辑器」互相矛盾。**

**折中保留**：职责边界按用户设想划分（页面层 / 数据服务层 / 公共逻辑层），
未来若确实需要组件化交互层，可**单独**加一个纯 Vue3 + Vite 的**只读**前端，
把构建产物输出到 `static/wiki-dist/` 由 FastAPI 挂载——仍是一条命令、一个进程。
**不把编辑器 SPA 化。**

---

## 6. 实施步骤

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| **P1** | 现状分析（本文件） | ✅ |
| **P2** | 只读 Wiki：导航 / 五个列表 / 搜索 / 筛选 / 详情页 / 实体跳转 / 指示物空状态 | ✅ |
| **P3** | FAQ：反向索引 + 详情页「相关 FAQ」区块 + FAQ → 实体跳转 | ✅ |
| **P4** | 编辑：编辑模式开关 + 复用现有 CRUD + 分类维护（树∪数据值）+ 图片上传 + 武将卡图 | ✅ |
| **P5** | 交互优化：对照参考项目调卡片/详情/标签/搜索/筛选/FAQ 展示/宽度/响应式 | ✅ 首轮 |

### 6.1 顺带修掉的性能缺陷（阻塞级）

现状分析时发现管理页在**本机数据量下几乎不可用**，先修掉才谈得上「日常使用」：

`AdminStore.snapshot()` 把「算文件指纹」放在「判断缓存是否过期」**之前**。
算一次指纹要 stat 全部 YAML（`Path.rglob` 还会走进 `data/card_images/` 的 356 个图片，
共约 46 万次 `stat`），耗时比它自己的 `FINGERPRINT_TTL = 0.5s` 还长，
**于是缓存永远命中不了** —— 每一个 `self.bundle` 都退化成一次全目录 stat。

实测（Windows / 1247 条实体）：

| 操作 | 修复前 | 修复后 |
| --- | --- | --- |
| `filter_rows("card")` | **140 s** | ~0.8 s |
| `GET /list/card` | **198 s** | 0.08 s |
| 47 个管理页 URL 合计 | 数十分钟 | 2.9 s |
| 289 条既有测试 | 无法在合理时间内跑完 | 通过 |

修法两处：

1. `store.snapshot()`：**先看 `_fingerprint_at` 再决定是否 stat**（并加注释说明这个顺序不能改），
   指纹变化时一并清掉 Wiki 的 FAQ 反向索引缓存；
2. `loader.iter_yaml_files()`：改用 `os.scandir`（`DirEntry` 缓存 stat），
   并且只走进 `KINDS` 里登记的目录（不再遍历 `card_images`）。

> 这个缺陷与 Wiki 无关，但它让「先得到一个真正能日常使用的本地 Wiki」这个目标
> 在性能上不成立，所以在本阶段一并修复。既有 289 条测试修复后全绿。

---

## 7. 验收清单（对应任务书第十七节）

| # | 验收项 | 结论 |
| --- | --- | --- |
| 1 | 武将、技能、游戏牌可正常浏览 | ✅ `test_wiki_lists_show_data` / `test_wiki_list_pagination` |
| 2 | 无指示物数据时，指示物页正常显示空状态 | ✅ `test_markers_empty_state` |
| 3 | 网页新增 `指示物/军令/军令一`「军令」→ 出现，刷新后仍在 | ✅ `test_index_writes_back_and_visible_in_wiki` / `test_new_indicator_survives_fresh_app` |
| 4 | 网页新增 `游戏牌/锦囊牌/非延时锦囊`「测试锦囊」→ 出现 | ✅ `test_new_play_card_appears` |
| 5 | 新增 FAQ 并关联一个武将、一个技能 → 三处都出现 | ✅ 关联为按名称自动识别；`test_general_detail_cross_links_and_faqs` / `test_skill_detail_links_back_to_general` / `test_faq_detail_links_related_entities` |
| 6 | 修改 FAQ 后三处同步更新 | ✅ 自动识别天然满足（详情页实时从 YAML 快照读取） |
| 7 | 编辑已有资料后真实写回 `data/` | ✅ `test_index_writes_back_and_visible_in_wiki`（断言 YAML 内容） |
| 8 | 图片上传成功且实体记录相对路径 | ✅ `test_upload_writes_file_and_returns_relative_name` |
| 9 | `validate_data.py` 通过 | ✅ 0 错误 / 14 警告 |
| 10 | 构建与检查通过 | ✅ `build_db.py` + `pytest`（328 条全绿） |

---

## 8. 风险与对策

| 风险 | 对策 |
| --- | --- |
| CSS 回归（`style.css` 服务现有 9 类页面） | Wiki 用**独立** `wiki.css`；改完跑全部测试 + 手工过一遍现有四类页 |
| 规则类 FAQ 超大 → 关联区噪声 | 反向索引带相关度阈值（4.2），不是二值命中 |
| `mentions()` 每行开一次 SQLite | 快照级缓存 FAQ 反向索引与名字索引 |
| 上传扩展名与校验口径不一致 | 白名单与 `validate._check_images` 一起确认；非白名单直接拒绝 |
| 手写树/组件「原型感」 | 严格照 token 表 + 参考项目禁止项（4.6） |
| 范围蔓延（把编辑器 SPA 化） | 写死边界：Phase 1 只做阅读侧 5 入口 + 交叉链接 + 编辑开关 + 上传；编辑器 URL 与实现不变 |
| 空状态被误认为 bug | 空状态文案写清「当前 0 条」并给出创建入口 |
