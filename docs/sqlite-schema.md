# SQLite 表结构（生成物，不需要手工维护）

本文件由 `build/sgs.db` 导出，用于查阅；**不要手工改数据库**。
修改表结构请改 `sgs_helper/db.py` 的 `COLUMNS` / `INDEXES` / `SCHEMA_VERSION`，
然后重新执行 `python scripts/build_db.py`。

- schema 版本：`3`（模型精简：删除通用备注、别名、手工关联、生效区间与存库的检索正文；
  实体卡类不再存 `source`；删掉独立的 `general_sets` 表）
- 生成时间：`2026-10-06T11:50:46+08:00`
- YAML 清单指纹：`744b78fb3ad36ce7bac81dd2ac6073ec45c86e7742a540de0f5e588e92b07e63`
- 全文检索 FTS5：`启用`

## 实体数量

| 实体类型（kind / 表） | 行数 |
| --- | --- |
| `product` | 1 |
| `general` | 155 |
| `general_variant` | 163 |
| `skill` | 311 |
| `skill_version` | 297 |
| `card` | 91 |
| `card_printing` | 192 |
| `faq` | 83 |
| `keyword` | 126 |
| `timing` | 30 |
| **合计** | **1449** |

来源表 `sources` 3 行（`physical_card` / `official_rule` / `official_online` 各一条）。

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
  verification_status TEXT,
  review_note TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `skills`

技能本体；文本与来源都在 `skill_versions`，这里只有名称与待复核说明。

```sql
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT,
  review_note TEXT,
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
  verification_status TEXT,
  review_note TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

没有 `package`、生效区间、变更说明、手工关联或通用备注这些列：
哪一版被使用由 `variant_skills.version_id` 显式绑定。

### `cards`

```sql
CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  name TEXT,
  card_type TEXT,
  subtype TEXT,
  review_note TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `card_printings`

`mode` 不落库（由 `product_id -> Product.mode` 推导），检索正文在构建时动态生成，
也**没有** `source` 列。

```sql
CREATE TABLE card_printings (
  id TEXT PRIMARY KEY,
  card_id TEXT,
  name_printed TEXT,
  product_id TEXT,
  card_set TEXT,
  suit TEXT,
  rank INTEGER,
  quantity INTEGER,
  printed_text TEXT,
  bottom_right_mark TEXT,
  card_number TEXT,
  image TEXT,
  verification_status TEXT,
  review_note TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `faqs`

```sql
CREATE TABLE faqs (
  id TEXT PRIMARY KEY,
  question TEXT,
  answer TEXT,
  mode TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

关联的武将 / 技能 / 卡牌 / 关键词 / 时点不落库：构建 `search_index` 与渲染详情时
按实体名称从 question/answer 里自动识别。

### `keywords`

```sql
CREATE TABLE keywords (
  id TEXT PRIMARY KEY,
  name TEXT,
  definition TEXT,
  mode TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `timings`

```sql
CREATE TABLE timings (
  id TEXT PRIMARY KEY,
  name TEXT,
  description TEXT,
  mode TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

时点用于“时点速查”，**没有**父子 / 前后 / 关联列。

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

- `idx_card_printings_card`：card_printings(card_id)
- `idx_card_printings_mark`：card_printings(bottom_right_mark)
- `idx_card_printings_product`：card_printings(product_id)
- `idx_faqs_mode`：faqs(mode)
- `idx_general_variants_general`：general_variants(general_id)
- `idx_general_variants_mode`：general_variants(mode)
- `idx_general_variants_product`：general_variants(product_id)
- `idx_keywords_mode`：keywords(mode)
- `idx_search_index_kind`：search_index(kind)
- `idx_skill_versions_prev`：skill_versions(previous_version_id)
- `idx_skill_versions_skill`：skill_versions(skill_id, mode)
- `idx_timings_mode`：timings(mode)
- `idx_variant_skills_skill`：variant_skills(skill_id)
- `idx_variant_skills_variant`：variant_skills(variant_id)

## 可追溯性列

每个实体表都带两列：

- `yaml_file`：数据来源文件，例如 `data/skills/skill_ji_zhi.yaml`、
  `data/general_variants/variant_cao_ren_guo_zhan_ban.yaml`；
- `yaml_line`：该条记录在文件中的起始行号。

只有还会保存来源的实体（`skill_versions` / `faqs` / `keywords` / `timings`）多一列
`source_key`，指向 `sources` 表；`products` / `general_variants` / `card_printings` 没有它
（来源由 `product_id` 定位），`generals` / `skills` / `cards` 也不需要。

`meta` 表另有 `git_commit` / `git_branch` / `git_dirty` / `yaml_manifest_sha256`，
可用于判断当前数据库是否与数据文件一致。
