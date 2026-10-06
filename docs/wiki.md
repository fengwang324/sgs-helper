# 本地 Wiki（`/wiki/*`）

> 目标：一个**能日常使用**的本地 Wiki —— 资料浏览器 + 本地编辑器 + UI 原型。
>
> Wiki 本身**不是**最终线上服务；数据始终以 `data/` 下的 YAML 为唯一权威源。
> 方案与现状分析见 [wiki-plan.md](wiki-plan.md)。

技术栈：继续沿用 `sgs_helper/admin/`（FastAPI + Jinja2 + 手写 CSS + 少量原生 JS）。
**没有**引入 Node / Vue / uni-app，也没有第二套写入实现 —— 理由见
[wiki-plan.md 第 5 节](wiki-plan.md)。

---

## 1. 启动

```bash
pip install -e ".[admin]"      # fastapi / uvicorn / jinja2 / python-multipart
python scripts/admin.py --port 8000
# 或： uvicorn sgs_helper.admin.app:app --host 127.0.0.1 --port 8000
```

打开 **<http://127.0.0.1:8000/wiki>**。

Wiki 与原来的管理页在**同一个进程、同一个端口**下：

| 地址 | 作用 |
| --- | --- |
| `/wiki` | Wiki 首页（资料检索） |
| `/list/{kind}`、`/view/{kind}/{id}`、`/new/*`、`/edit/*`、`/delete/*` | 原管理页（录入编辑器仍在用） |

---

## 2. 页面结构

### 一级入口

| 路由 | 数据 | 说明 |
| --- | --- | --- |
| `/wiki/generals` | `general` + `general_variant` | 武将 |
| `/wiki/skills` | `skill` + `skill_version` | 技能 |
| `/wiki/cards` | `card` where `category_1 == 游戏牌` | 游戏牌 |
| `/wiki/markers` | `card` where `category_1 == 指示物` | 指示物 |
| `/wiki/faq` | `faq` | FAQ / 裁定 |
| `/wiki/rules` | — | **预留**：只说明规则关键词 / 时点已有多少条，不做内容 |
| `/wiki/search?q=` | 全部 | 全库搜索（跨四类，按类型分组） |

列表页支持：`q`（搜索）、`mode`、`status`（核验状态）、`set_name`（武将牌系列 /
拓展包）、`category_2`、`category_3`、`page`（分页，每页 48 条）。

### 详情页与互跳

| 详情页 | 展示 | 能跳去哪 |
| --- | --- | --- |
| 武将 `/wiki/generals/{id}` | 卡面、称号、名字、势力、体力、性别、模式、编号、武将牌系列、产品、技能、变体 | **技能**（点技能名）、相关 FAQ、数据来源 |
| 技能 `/wiki/skills/{id}` | 技能名、各版本描述（模式 / 产品 / 来源 / 核验 / 上一版）、所属武将 | **武将**、相关 FAQ |
| 卡牌 `/wiki/card/{id}` | 卡图、一/二/三级分类、拓展包、效果文本、关联技能 | **技能**、相关 FAQ |
| 指示物（同上，靠 `category_1` 区分） | 同上 | 同上 |
| FAQ `/wiki/faq/{id}` | 问题、答案（正文内资料名可点击）、模式、来源、核验状态 | 相关武将 / 技能 / 卡牌 / 关键词 / 时点 |

**详情页尽量让用户直接点，不用回首页重新搜。**

---

## 3. 武将的建模约定（重要）

**一个武将 = 名字 + 称号**，两者都相同才算同一个武将：

- `诸葛亮 / 迟暮的丞相` 与 `诸葛亮 / 迟暮的丞相` → 同一个武将（不同印刷才是多版本）
- `司马昭 / 天下畏威`（晋，G.JIN 004）与 `司马昭 / 嘲风开天`（野心家，G.AM 001）
  → **两个不同的武将**，各自一张卡、各自一个详情页
- `曹操 / 魏武帝`（G.WEI 001）与 `曹操 / 电模`（G.EM 004）→ 同样是两个武将

当前数据：**163 个武将 = 163 张卡**，每个武将恰好一个版本。

### 卡面编号决定内部 id 与卡图名

卡面编号（`general_number`，如 `G.WEI 001`）在数据里唯一，所以它同时派生两样东西：

| 派生 | 规则 | 例 |
| --- | --- | --- |
| 内部 id | `variant_` + 编号（小写，**非字母数字→下划线**） | `variant_g_wei_001` |
| 卡图名 | 同一规则 + `.jpg`（相对 `data/card_images/`） | `g_wei_001.jpg` |

- `G.WEI/SHU 079` → `variant_g_wei_shu_079` / `g_wei_shu_079.jpg`
- `G-JIN 047` → `g_jin_047`（连字符与点号结果一致）

在武将录入页**只改编号**，内部 id 与卡图名会一起跟着变（旧图存在时会一起改名），
不需要手工维护。所以编辑页不再有「高级信息（内部 ID / 复核备注）」下拉栏。

`variant`（版本标签）统一为产品名「群狼环鼎」，**已不再有区分度**，
Wiki 上改用「称号 · 卡面编号」来区分卡片。

---

## 4. 分类与来源是两套独立信息

- **三级分类**回答「这张卡是什么」：`category_1/2/3`。
- **来源**回答「它来自哪个产品 / 拓展包」：`Card.card_set` / `GeneralVariant.product_id`。

来源不会改变一张卡本身属于什么分类。Wiki 的「游戏牌」与「指示物」两个入口
**直接按 `category_1` 筛选**。

当前分类树（`constants.CARD_CATEGORY_TREE`）：

```text
游戏牌
├─ 基本牌（category_3 是牌族：杀 / 闪 / 桃 / 酒，具体哪一张写在 name）
├─ 锦囊牌（非延时锦囊 / 延时锦囊）
└─ 装备牌（武器 / 防具 / 坐骑 / 宝物）

指示物
├─ 军令
├─ 鏖战事件
├─ 技能指示物
├─ 衍生技能牌
└─ 通用标记
```

不同分支的 `category_3` **允许有不同语义粒度**（游戏牌→基本牌下面是牌族；
指示物→军令下面是具体名称）。

### 新增分类不需要改代码

Wiki 的分类树 = **内置分类树 ∪ 数据里实际出现过的取值**。

所以在卡牌录入页的二级 / 三级分类里**直接输入一个新名字并保存，就等于新建了这个分类**，
下一次渲染 Wiki 导航树立刻出现它。卡牌录入页的分类候选也是同样两个来源。

> 代价：没有被任何卡引用过的分类不会预先显示。指示物 5 个二级分类来自内置树，
> 所以即使 0 条数据，空状态页也能列出它们。

---

## 5. 编辑模式

默认是**浏览态**。点右上角「✎ 编辑模式：关」开启：

- **列表页**：右上角出现「+新增」（卡牌 / 武将 / 技能 / FAQ）；
- **详情页**：出现「编辑 / 复制 / 删除」；
- 指示物空状态页出现「+新增第一条」。

开关走 cookie `sgs_edit=1`（服务端读；30 天有效），`POST /wiki/edit-mode` 带 CSRF。
**没有 JS 也能用**（表单提交 → 303 回原页）。

Wiki **不重写写入逻辑**，编辑 / 新增 / 删除全部复用现有的：

| 动作 | 复用路由 |
| --- | --- |
| 新增武将 | `/new/general` |
| 新增技能 | `/new/skill` |
| 新增卡牌 / 指示物 | `/new/card`（带 `?category_1=` 预填） |
| 新增 FAQ | `/new/faq` |
| 编辑 / 删除 | `/edit/{kind}/{id}`、`/delete/{kind}/{id}` |
| 复制 | `/new/{kind}?from={id}`（武将用现成的「以此为模板新增」） |

所以下面这些**已经存在的能力**依然生效，不需要在 Wiki 里重新实现：

- 保存前在内存里把改动合进完整数据集跑一遍完整校验，**不通过一个字都不落盘**；
- 临时文件 + fsync + `os.replace` 原子落盘；
- 内容哈希 + CSRF token 防并发覆盖；
- 一次提交只 rebuild 一次 SQLite；
- 删除前检查引用关系，被引用就拒绝。

### 数据流向（永远只有这一条）

```text
浏览器编辑 → 写回 YAML → validate → build_db → SQLite 更新
```

Wiki **不会**直接改 SQLite。

---

## 6. 新增第一条指示物（当前数据库里是 0 条，这是正常状态）

1. 打开 <http://127.0.0.1:8000/wiki/markers>，点「✎ 开启编辑模式并新增」；
2. 一级分类已预填 `指示物`，二级分类选 `军令`，三级分类填 `军令一`；
3. 卡名填 `军令`，保存；
4. 回到 `/wiki/markers`：该条立刻出现，**刷新浏览器依然在**。

也可以直接在地址栏打开
<http://127.0.0.1:8000/new/card?category_1=%E6%8C%87%E7%A4%BA%E7%89%A9>。

---

## 7. 图片上传

- 入口：卡牌录入页「上传卡图」、武将录入页「卡图」旁的「上传卡图」。
- 接口：`POST /api/upload-image`（multipart）。
- 落到 `data/card_images/`，**实体里只存文件名**（相对 `data/card_images/`）。
  这是 `Card.image` / `GeneralVariant.image` 的字段语义，不是完整路径。
- 限制：`jpg / jpeg / png / webp`，单张 ≤ 8MB，文件名安全化，重名自动加 `_1` `_2`。
- 非白名单扩展名直接 400（否则会让 `validate._check_images` 在保存时失败）。

> 武将卡图字段（`GeneralVariant.image`）以前在页面上**完全无法编辑**，这次补上了。

---

## 8. FAQ 的关联是怎么来的（重要）

FAQ **没有** `related_*` 手工字段。关联一律由「**实体名称在 FAQ 正文里出现**」自动识别：

- 建库：`db.build_name_index()` / `db.detect_mentions()` 写进 `search_index.body`；
- 查询：`repo.mentions(conn, text, kinds, limit)`（最长名称优先，名称长度 < 2 不参与）；
- 页面：`store.mentions()` / `store.annotate()` 把正文切成「普通文本 / 可点击实体」。

新增的只有**反向索引** `store.related_faqs(kind, id)`，因为原来只有
「给文本找实体」，没有「给实体找 FAQ」。

### 为什么要算相关度（而不是「命中就算」）

规则类 FAQ 极大 —— 例如 `faq_rule_flow_damage.yaml` 的答案有数千字、罗列上百个技能名。
如果按「命中=相关」做反向索引，**几乎每个技能都会挂上同一批规则专题**，关联区被噪声淹没。

所以按命中位置打分（`store._faq_relevance`）：

| 命中位置 | 分数 |
| --- | --- |
| 在 `question` 里 | 100（用户问的就是它） |
| 在 `answer` 开头 200 字内 | 50 |
| 在 `answer` 前半段 | 20 |
| 只在超长正文的后半段 | 不计 |

低于 20 分不算相关；详情页只显示**相关度最高的前 6 条**。
比对一律按 `(kind, id)`，不按名称，避免同名误关联。

索引按文件指纹缓存（`store._faq_index`），83 条 FAQ 建一次约 0.2 秒，之后 0 成本。

> 页面上会标注「关联为按名称自动识别，可能误报 / 漏报」。真的需要精确控制时，
> 正确做法是给实体加 `aliases` 字段，而不是加回手工关联字段。

---

## 9. 视觉规范

对齐参考项目 open-shap2-web 的 Wiki（见 `wiki-plan.md` 第 4.6 节）。token：

```css
--page-p: 28px;  --page-inset: 20px;  --page-gap: 16px;  --page-r: 12px;
/* 内容区 max-width: 1080px；白底、黑主文字、浅灰边框、圆角卡片 */
```

硬规则：

- panel **只有一层 border**；内层 inset **只给底色 + 圆角，不再套 border**；
- 禁止盒中盒（panel 套 card 套 inset）；
- 大区域**禁用主题色 wash 铺底**；
- 内容页标题 H1 24px/700（不用 hero 大标题）；H2 19px/800 + 左侧 5px 竖条；
- 正文 14px / `line-height: 1.75`。

样式在**新增的** `static/wiki.css` 里，现有 `static/style.css` 继续服务原来的 9 类管理页面，
两者互不影响。

---

## 10. 代码位置

| 文件 | 作用 |
| --- | --- |
| `sgs_helper/admin/wiki.py` | `/wiki/*` 的全部页面逻辑（列表 / 详情 / 搜索 / 编辑模式 / 上传） |
| `sgs_helper/admin/templates/wiki/` | `base.html`、`home.html`、`list.html`、`search.html`、`detail.html`、`rules.html`、`_tabs.html`、`_category_tree.html` |
| `sgs_helper/admin/static/wiki.css` | Wiki 专用样式（新文件，不动旧 CSS） |
| `sgs_helper/admin/static/wiki-upload.js` | 卡图上传（新文件） |
| `sgs_helper/admin/store.py` | 新增 `wiki_nav_tree()`、`related_faqs()`、`related_faq_counts()`、`filter_options_scoped()` |
| `sgs_helper/constants.py` | `CARD_CATEGORY_TREE` 补指示物子树；新增 `CARD_CATEGORY_1_FOR_WIKI` |
| `sgs_helper/validate.py` | 新增「指示物二级分类不在已知子树」告警 |
| `tests/test_wiki.py` | Wiki 测试（39 条） |

---

## 11. 已知边界

- 规则页只预留入口，不做内容（按任务书要求）。
- 同一规则卡因花色 / 点数产生的多份实体印刷**不在 Wiki 展示**：`Card` 只有一层，
  完整牌堆构成留给以后的 `deck_entries`。
- FAQ 关联是「按名称自动识别」，可能有误报 / 漏报；页面上有标注。
- 编辑模式只有「开 / 关」，没有权限体系（本地工具，无登录）。
- 分类树里没有被任何卡引用过的分类不会显示（指示物的内置子树除外）。
- 录入编辑器（`/new/*`、`/edit/*`）仍是原来的管理页版式，没有换成 Wiki 样式。
