# SQLite 表结构（生成物，不需要手工维护）

本文件由 `build/sgs.db` 导出，用于查阅；**不要手工改数据库**。
修改表结构请改 `sgs_helper/db.py` 的 `COLUMNS` / `INDEXES` / `SCHEMA_VERSION`，
然后重新执行 `python scripts/build_db.py`。

- schema 版本：`2`（武将拆成 `generals` + `general_variants`，技能关系表改名 `variant_skills`）
- 生成时间：`2026-10-02T04:18:02+00:00`
- YAML 清单指纹：`e9008bf3c0dd9f68f57631b5c468617a07c641f1b198d80fc6e6884ba9fc6018`
- 全文检索 FTS5：`启用`

## 实体数量

| 实体类型（kind / 表） | 行数 |
| --- | --- |
| `card` | 4 |
| `card_printing` | 6 |
| `faq` | 5 |
| `general` | 3 |
| `keyword` | 10 |
| `product` | 2 |
| `skill` | 6 |
| `skill_version` | 9 |
| `timing` | 7 |

## 表

### `card_printings`

```sql
CREATE TABLE card_printings (
  id TEXT PRIMARY KEY,
  card_id TEXT,
  name_printed TEXT,
  mode TEXT,
  product_id TEXT,
  package TEXT,
  card_set TEXT,
  suit TEXT,
  rank INTEGER,
  quantity INTEGER,
  printed_text TEXT,
  normalized_text TEXT,
  bottom_right_mark TEXT,
  card_number TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `cards`

```sql
CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  name TEXT,
  card_type TEXT,
  subtype TEXT,
  aliases TEXT,
  related_keywords TEXT,
  notes TEXT,
  source_key TEXT,
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
  related_general_ids TEXT,
  related_skill_ids TEXT,
  related_card_ids TEXT,
  related_keywords TEXT,
  related_timing_ids TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `fts_search`

```sql
CREATE VIRTUAL TABLE fts_search USING fts5(
  kind UNINDEXED, entity_id UNINDEXED, text, tokenize='unicode61'
);
```

### `fts_search_config`

```sql
CREATE TABLE 'fts_search_config'(k PRIMARY KEY, v) WITHOUT ROWID;
```

### `fts_search_content`

```sql
CREATE TABLE 'fts_search_content'(id INTEGER PRIMARY KEY, c0, c1, c2);
```

### `fts_search_data`

```sql
CREATE TABLE 'fts_search_data'(id INTEGER PRIMARY KEY, block BLOB);
```

### `fts_search_docsize`

```sql
CREATE TABLE 'fts_search_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
```

### `fts_search_idx`

```sql
CREATE TABLE 'fts_search_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
```

### `variant_skills`

武将**版本** → 技能的关系表（由 ``GeneralVariant.skills`` 生成；``version_id`` 是武将显式绑定的技能版本，可为 NULL；
``general_id`` 冗余存一份，方便按武将统计）。

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

### `generals`

```sql
CREATE TABLE generals (
  id TEXT PRIMARY KEY,
  name TEXT,
  aliases TEXT,
  notes TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `general_variants`

某个玩法/版本下的武将数据（模式、势力、体力、称号、编号、技能都在这里）。

```sql
CREATE TABLE general_variants (
  id TEXT PRIMARY KEY,
  general_id TEXT,
  variant TEXT,
  title TEXT,
  general_number TEXT,
  mode TEXT,
  faction TEXT,
  hp REAL,
  gender TEXT,
  product_id TEXT,
  general_set TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `keywords`

```sql
CREATE TABLE keywords (
  id TEXT PRIMARY KEY,
  name TEXT,
  definition TEXT,
  aliases TEXT,
  mode TEXT,
  related_keyword_ids TEXT,
  related_timing_ids TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `meta`

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
```

### `products`

```sql
CREATE TABLE products (
  id TEXT PRIMARY KEY,
  name TEXT,
  series TEXT,
  mode TEXT,
  publisher TEXT,
  release_year INTEGER,
  release_date TEXT,
  edition TEXT,
  general_sets TEXT,
  card_sets TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `search_index`

```sql
CREATE TABLE search_index (
  kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  name TEXT, aliases TEXT, body TEXT,
  PRIMARY KEY (kind, entity_id)
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
  package TEXT,
  effective_from TEXT,
  effective_to TEXT,
  previous_version_id TEXT,
  change_note TEXT,
  related_keywords TEXT,
  related_timing_ids TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `skills`

```sql
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT,
  aliases TEXT,
  notes TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `sources`

```sql
CREATE TABLE sources (
  source_key TEXT PRIMARY KEY,
  type TEXT, title TEXT, reference TEXT, notes TEXT
);
```

### `timings`

```sql
CREATE TABLE timings (
  id TEXT PRIMARY KEY,
  name TEXT,
  description TEXT,
  mode TEXT,
  parent_id TEXT,
  previous_ids TEXT,
  next_ids TEXT,
  related_keywords TEXT,
  related_skill_ids TEXT,
  related_faq_ids TEXT,
  notes TEXT,
  verification_status TEXT,
  source_key TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

## 索引

- `idx_card_printings_card`：card_printings(card_id)
- `idx_card_printings_mark`：card_printings(bottom_right_mark)
- `idx_card_printings_product`：card_printings(product_id)
- `idx_faqs_mode`：faqs(mode)
- `idx_general_variants_general`：general_variants(general_id)
- `idx_general_variants_mode`：general_variants(mode)
- `idx_general_variants_product`：general_variants(product_id)
- `idx_variant_skills_variant`：variant_skills(variant_id)
- `idx_keywords_mode`：keywords(mode)
- `idx_search_index_kind`：search_index(kind)
- `idx_skill_versions_prev`：skill_versions(previous_version_id)
- `idx_skill_versions_skill`：skill_versions(skill_id, mode)
- `idx_skill_versions_window`：skill_versions(skill_id, effective_from, effective_to)
- `idx_timings_mode`：timings(mode)

## 可追溯性列

每个实体表都带三列：

- `source_key`：指向 `sources` 表（YAML 内嵌 source 结构去重后的结果）；
- `yaml_file`：数据来源文件，例如 `data/skills/skill_ji_zhi.yaml`、`data/general_variants/variant_zhu_ge_liang_biao_zhun_ban.yaml`；
- `yaml_line`：该条记录在文件中的起始行号。

`meta` 表另有 `git_commit` / `git_branch` / `git_dirty` / `yaml_manifest_sha256`，
可用于判断当前数据库是否与数据文件一致。
