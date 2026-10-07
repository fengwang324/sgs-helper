# SQLite 表结构（生成物，不需要手工维护）

本文件由 `build/sgs.db` 导出，用于查阅；**不要手工改数据库**。
修改表结构请改 `sgs_helper/db.py` 的 `COLUMNS` / `INDEXES` / `SCHEMA_VERSION`，
然后重新执行 `python scripts/build_db.py`。

- schema 版本：`4`（卡牌单层化：游戏牌收敛成一层 `Card`；删除整个 `CardPrinting` 层、
  `card_printings` 表与它的三个索引；新增 `idx_cards_category` / `idx_cards_card_set`；
  `Product.card_sets` 只表示游戏牌拓展包）
- 生成时间：`2026-10-06T16:00:19+08:00`
- YAML 清单指纹：`ed863c0903e0b7a52222f392d89a5055a08c406e88dbabe3f09e4cfef6202ab7`
- 全文检索 FTS5：`启用`

## 实体数量

| 实体类型（kind / 表） | 行数 |
| --- | --- |
| `product` | 1 |
| `general` | 155 |
| `general_variant` | 163 |
| `skill` | 311 |
| `skill_version` | 297 |
| `card` | 81 |
| `faq` | 85 |
| `keyword` | 117 |
| **合计** | **1218** |

来源表 `sources` 2 行（`physical_card` 与 `official_online` 各一条）。
FAQ / 关键词不再贡献来源行：FAQ 的来源是多行文本列，关键词没有来源字段。

## 表

### `products`

```sql
CREATE TABLE products (
  id TEXT PRIMARY KEY,
  name TEXT,
  release_year INTEGER,
  mode TEXT,
  general_sets TEXT,
  card_sets TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

`general_sets` / `card_sets` 是 JSON 文本（多值列）。

### `generals`

```sql
CREATE TABLE generals (
  id TEXT PRIMARY KEY,
  name TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `general_variants`

某个玩法 / 版本下的武将数据；来源由 `product_id` 定位，**没有** `source`。
`zhulianbihe` 是 JSON 文本。

```sql
CREATE TABLE general_variants (
  id TEXT PRIMARY KEY,
  general_id TEXT,
  variant TEXT,
  title TEXT,
  general_number TEXT,
  illustrator TEXT,
  image TEXT,
  mode TEXT,
  faction TEXT,
  hp REAL,
  gender TEXT,
  product_id TEXT,
  general_set TEXT,
  zhulianbihe TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `skills`

技能本体；文本与来源都在 `skill_versions`，这里只有 `id` / `name`。

```sql
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `skill_versions`

```sql
CREATE TABLE skill_versions (
  id TEXT PRIMARY KEY,
  skill_id TEXT,
  mode TEXT,
  text TEXT,
  version_label TEXT,
  product_id TEXT,
  previous_version_id TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

没有 `package`、生效区间、变更说明、手工关联或通用备注这些列：
哪一版被使用由 `variant_skills.version_id` 显式绑定。

### `cards`

一层的游戏牌（与指示物共用这一层）：三级分类表达“这张卡是什么”，`card_set` 表达
“它来自哪个拓展包”（允许为空），`effect_text` / `effect_skills` 是卡面效果与关联技能。
分类取值**自由填写**：只有 `category_1` 必填，`category_2` / `category_3` 允许为空字符串；
`constants.CARD_CATEGORY_TREE` 只是录入候选项（没有校验白名单）。习惯上基本牌的
`category_3` 写牌族（杀 / 闪 / 桃 / 酒），具体哪一张写在 `name`。

```sql
CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  category_1 TEXT,
  category_2 TEXT,
  category_3 TEXT,
  name TEXT,
  image TEXT,
  card_set TEXT,
  effect_text TEXT,
  effect_skills TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

`effect_text` 就是 `Card.effect.text`；`effect_skills` 是 `Card.effect.skills`
（`[{skill_id, version_id}]`）的 JSON 文本，查询层在 `repo._card_effect_skills` 里解析。
卡与产品**没有外键**：两边的唯一关系是 `Card.card_set ∈ Product.card_sets`。
**没有** `card_printings` 表（`CardPrinting` 层与 `data/card_printings/` 目录已删除）：
同一规则卡的花色 / 点数印刷不在这一层建模，完整牌堆构成留给以后的 `deck_entries`。

### `faqs`

```sql
CREATE TABLE faqs (
  id TEXT PRIMARY KEY,
  question TEXT,
  answer TEXT,
  variant TEXT,
  source TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

关联的武将 / 技能 / 卡牌 / 关键词不落库：构建 `search_index` 与渲染详情时
按实体名称从 question/answer 里自动识别。

### `keywords`

```sql
CREATE TABLE keywords (
  id TEXT PRIMARY KEY,
  name TEXT,
  definition TEXT,
  variant TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `variant_skills`

武将**版本** → 技能的关系表（由 `GeneralVariant.skills` 生成；`version_id` 是武将显式绑定的
技能版本，可为 NULL；`general_id` 冗余存一份，方便按武将统计）。

```sql
CREATE TABLE variant_skills (
  variant_id TEXT NOT NULL,
  general_id TEXT NOT NULL,
  skill_id TEXT NOT NULL,
  version_id TEXT,
  position INTEGER NOT NULL,
  PRIMARY KEY (variant_id, skill_id)
);
```

### `sources`

只剩 `type` + `reference`；展示用的来源名在渲染时由 `reference` 推导，
因此 `sources` 没有标题、备注或别名字段。

```sql
CREATE TABLE sources (
  source_key TEXT PRIMARY KEY,
  type TEXT, reference TEXT
);
```

### `search_index`

```sql
CREATE TABLE search_index (
  kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  name TEXT, body TEXT,
  PRIMARY KEY (kind, entity_id)
);
```

`body` 在构建时拼好，含“按实体名称自动识别出来的关联对象名”（替代原来的手工关联字段）；
表里只有 `(kind, entity_id, name, body)`，没有任何别名列。

### `fts_search`

```sql
CREATE VIRTUAL TABLE fts_search USING fts5(
  kind UNINDEXED, entity_id UNINDEXED, text, tokenize='unicode61'
);
```

FTS5 还会自动创建 `fts_search_config` / `fts_search_content` / `fts_search_data` /
`fts_search_docsize` / `fts_search_idx` 这几张内部表；环境没有 FTS5 时这些表不存在，
查询层自动回退 `LIKE`。

### `meta`

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```

## 索引

- `idx_cards_card_set`：cards(card_set)
- `idx_cards_category`：cards(category_1, category_2)
- `idx_faqs_variant`：faqs(variant)
- `idx_general_variants_general`：general_variants(general_id)
- `idx_general_variants_mode`：general_variants(mode)
- `idx_general_variants_product`：general_variants(product_id)
- `idx_keywords_variant`：keywords(variant)
- `idx_search_index_kind`：search_index(kind)
- `idx_skill_versions_prev`：skill_versions(previous_version_id)
- `idx_skill_versions_skill`：skill_versions(skill_id, mode)
- `idx_variant_skills_skill`：variant_skills(skill_id)
- `idx_variant_skills_variant`：variant_skills(variant_id)

## 可追溯性列

每个实体表都带两列：

- `yaml_file`：数据来源文件，例如 `data/skills/skill_ji_zhi.yaml`、
  `data/general_variants/variant_cao_ren_guo_zhan_ban.yaml`；
- `yaml_line`：该条记录在文件中的起始行号。

只有 `skill_versions` 多一列 `source_key`，指向 `sources` 表（结构化的 `type` + `reference`）；
`faqs` 的来源是文本列 `source`，`keywords` 没有来源列，
`products` / `general_variants` / `cards` 没有 `source_key`
（来源由 `product_id` / `card_set` 定位），`generals` / `skills` 也不需要。

`meta` 表另有 `git_commit` / `git_branch` / `git_dirty` / `yaml_manifest_sha256`，
可用于判断当前数据库是否与数据文件一致。
