# SQLite 表结构（生成物）

YAML 是唯一权威源。修改 `sgs_helper/schema.py` 与 `sgs_helper/db.py` 后，运行 `python scripts/build_db.py` 全量重建；不要直接修改数据库。

- schema 版本：`8`
- 生成时间：`2026-10-12T00:05:23+08:00`
- YAML 清单指纹：`312d8dbd34dd7329042430053d14f35f9322fd7585be92464d03859bc6a340fd`
- 全文检索 FTS5：启用

## 实体数量

| 类型 | 表 | 行数 |
| --- | --- | ---: |
| `product` | `products` | 1 |
| `general` | `generals` | 163 |
| `general_variant` | `general_variants` | 163 |
| `skill` | `skills` | 291 |
| `skill_version` | `skill_versions` | 291 |
| `card` | `cards` | 114 |
| `faq` | `faqs` | 25 |
| `keyword` | `keywords` | 43 |
| **合计** | | **1091** |

## 字段约定

- `general_variants` 保留产品、玩法与武将牌组；产品名称由 `product_id` 取得，不再存 `variant`。同名但编号或称号不同的武将独立保留。
- `skill_versions` 仅维护技能 ID、模式、产品和正文；没有重复标签、来源列或前一版本链。武将通过 `variant_skills.version_id` 绑定具体文本。
- `cards.effect` 是效果正文文本；武器的 `attack_range` 在外层，坐骑距离仅写在正文中。
- `cards.deck_info` 是游戏牌的牌堆数量、花色及点数文本；空字符串表示待录入，指示物为 SQL NULL。
- `cards.rules` 是基本牌 / 锦囊牌的使用方法 JSON，只含 `use_methods`，各方法允许 `label`、`timing`、`target`、`effect`；空字段省略。装备和指示物为 SQL NULL。
- `cards.note` 是可选资料备注，补充裁定归 FAQ；卡牌没有技能引用。
- `cards.recast` 为布尔标记，SQLite使用INTEGER；通用说明归重铸关键词，不复制成每张牌的使用方法。
- `card_set` 只用于游戏牌，关联方式为 `Card.card_set ∈ Product.card_sets`；指示物为 SQL NULL。
- `general_sets`、`card_sets`、`zhulianbihe` 是 JSON 数组；同一规则卡不因花色和点数拆成多个记录。
- FAQ 的 `source` 仍是多行文本；没有独立 `sources` 表。

## 表结构

### `cards`

```sql
CREATE TABLE cards (
  id TEXT PRIMARY KEY,
  category_1 TEXT,
  category_2 TEXT,
  category_3 TEXT,
  name TEXT,
  image TEXT,
  card_set TEXT,
  effect TEXT,
  attack_range TEXT,
  deck_info TEXT,
  note TEXT,
  recast INTEGER,
  rules TEXT,
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
  variant TEXT,
  source TEXT,
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

### `general_variants`

```sql
CREATE TABLE general_variants (
  id TEXT PRIMARY KEY,
  general_id TEXT,
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

### `generals`

```sql
CREATE TABLE generals (
  id TEXT PRIMARY KEY,
  name TEXT,
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
  variant TEXT,
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
  release_year INTEGER,
  mode TEXT,
  general_sets TEXT,
  card_sets TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
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

### `skill_versions`

```sql
CREATE TABLE skill_versions (
  id TEXT PRIMARY KEY,
  skill_id TEXT,
  mode TEXT,
  text TEXT,
  product_id TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `skills`

```sql
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT,
  yaml_file TEXT,
  yaml_line INTEGER
);
```

### `variant_skills`

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

FTS5 的内部表由 SQLite 自动管理；环境不支持 FTS5 时，查询回退到 LIKE。

## 索引

- `idx_cards_card_set`：`CREATE INDEX idx_cards_card_set ON cards(card_set)`
- `idx_cards_category`：`CREATE INDEX idx_cards_category ON cards(category_1, category_2)`
- `idx_faqs_variant`：`CREATE INDEX idx_faqs_variant ON faqs(variant)`
- `idx_general_variants_general`：`CREATE INDEX idx_general_variants_general ON general_variants(general_id)`
- `idx_general_variants_mode`：`CREATE INDEX idx_general_variants_mode ON general_variants(mode)`
- `idx_general_variants_product`：`CREATE INDEX idx_general_variants_product ON general_variants(product_id)`
- `idx_keywords_variant`：`CREATE INDEX idx_keywords_variant ON keywords(variant)`
- `idx_search_index_kind`：`CREATE INDEX idx_search_index_kind ON search_index(kind)`
- `idx_skill_versions_skill`：`CREATE INDEX idx_skill_versions_skill ON skill_versions(skill_id, mode)`
- `idx_variant_skills_skill`：`CREATE INDEX idx_variant_skills_skill ON variant_skills(skill_id)`
- `idx_variant_skills_variant`：`CREATE INDEX idx_variant_skills_variant ON variant_skills(variant_id)`

## 可追溯性

每个实体都保存 `yaml_file` 与 `yaml_line`。`meta` 记录 schema 版本、构建时间、Git commit/branch/dirty、实体数量以及 YAML 清单指纹，便于核对生成物是否对应当前数据。
