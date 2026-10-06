# 维护手册（日常怎么改数据）

## 0. 标准流程

两条等价的路：**浏览器里用本地管理页录**（推荐，见 [admin.md](admin.md)），
或者直接在 YAML 里改。页面做的事和下面的命令完全一样，只是帮你把 YAML 写好。

```bash
cd /data/wf/1002/sgs-helper

# ① 改 YAML（新增文件 / 修改文件都行；data/ 是唯一权威源）
#    或者： python scripts/admin.py --port 8000  → 浏览器里录，保存即自动 validate + build
# ② 校验
python scripts/validate_data.py
# ③ 重建数据库
python scripts/build_db.py
# ④ 测试
python -m pytest -q
# ⑤ 提交
git add -A && git commit -m "data: ..." && git push
```

任何时候都**不要**直接改 `build/sgs.db`：它是生成物，下次构建就被覆盖。

## 0.1 录一张实体武将牌（推荐路径）

1. 先有产品：`/new/product` —— 名称 / 发行年份 / 模式 / **武将牌系列**（`general_sets`）/
   **游戏牌牌堆**（`card_sets`）。
2. 再录武将：`/new/general` —— **只有名字必填**。
   - 「编号」是卡面编号（`general_number`，如 `WEI001`），内部 ID 留空会自动生成；
   - 技能行里搜索已有技能 → 选择这个武将用的**版本**（不选则按 `mode` 匹配该技能唯一一条版本）；
   - 没有的技能点 `+ 新建技能「xxx」`，填名称（只建技能本体）或再加描述（同时建技能版本）；
   - 需要改技能文本时用「+ 为这个技能新增版本」，旧版本永远保留。
3. 同一盒卡连续录：用「保存并新增下一位武将」（可继承产品信息），
   或详情页的「以此武将为模板新增」。
4. 录完随时看 `/review`，把 `unverified` / `community` / `conflict` 的记录逐条核验掉。

## 1. 现在的数据构成

规则侧（技能 / FAQ / 关键词 / 时点）从
[《三国杀官方规则集 3.0》网页版](https://gltjk.com/sanguosha/rules/)导入，
武将 / 卡牌侧来自群狼环鼎（`qlhd`）实体卡照片：

| 目录 | 现状 | 来源 |
| --- | --- | --- |
| `data/skills/`（311）、`data/skill_versions/`（297） | 真实技能文本 | 群狼环鼎卡面 OCR（296 条）+ 规则集武将页（1 条） |
| `data/faq/`（83） | 真实裁定 | 规则集专题/流程章节 + 武将页 ◆ 裁定 |
| `data/keywords/`（126）、`data/timings/`（30） | 真实定义 | 规则集「用语」「游戏的流程」 |
| `data/generals/`（155）、`data/general_variants/`（163） | 真实数据，全是国战版本 | 群狼环鼎卡面 OCR + [三国杀WIKI（bilibili）](https://wiki.biligame.com/sgs/) |
| `data/products/`（1）、`data/cards/`（91）、`data/card_printings/`（192） | 真实数据（产品 `qlhd`） | 群狼环鼎实体卡 |

实测（`validate_data.py`）：**实体 1449 条 / 0 错误 / 60 警告**。

重新导入 / 增量导入（站点更新时）：

```bash
pip install pypinyin                                    # 可选：生成拼音 id
bash scripts/import_rules_book.sh                       # 下载页面到 /tmp/rules
python scripts/import_rules_book.py --stage all         # 写 data/（会覆盖同名 id）
python scripts/validate_data.py && python scripts/build_db.py && python -m pytest -q
```

导入的规则侧统一带 `source.type: official_rule` + `verification_status: unverified`
（网页版是社区整理、未按实体卡核验）。**核验一条就升一条**：对着实体卡逐字比对之后，
把 `verification_status` 改成 `physical_verified`；需要记差异就写 `review_note`
（没有该字段的 FAQ / Keyword / Timing 写进正文）。

## 1.1 武将是两层：General + GeneralVariant

武将数据由脚本 `scripts/import_generals_wiki.py` 从 WIKI 导入（只取「经典版本」段 = 标准身份版）。

- `General`（`data/generals/`）：只有 `id` / `name`。
  官方武将名不同就是不同的武将个体（诸葛亮 / 界诸葛亮 / 神诸葛亮）。
- `GeneralVariant`（`data/general_variants/`）：`variant`（标准版 / 国战版 / 界限突破 …）
  + 模式、势力、体力、性别、称号、编号、武将牌系列、产品、技能。
  官方名相同、玩法不同 → 同一个 General 的多个 Variant。
  来源不在这里存：由 `product_id` 定位到具体产品。
- 体力口径：身份局标准版记正常体力（3/4/5）；国战按阴阳鱼记 1.5 / 2 / 2.5
  （**不要**把半阴阳鱼记成 3/4/5）。
- `general_set`：只写一个具体系列名，候选来自所属产品的 `Product.general_sets`
  （没有独立的「系列」实体）；`skills[].version_id` 为空时按该武将版本的 `mode`
  匹配该技能唯一一条版本。
- 导入时**只录明确属于标准版的部分**，拿不准的不猜：
  萌娘百科的「标准版」名单与 WIKI 的 `武将包` 不一致的（例如袁术，WIKI 归在神话再临·雷包），
  先不录，留待按实体卡确认。

## 1.2 再录一批实体卡（产品 / 卡牌）

1. **先建产品**（`data/products/` 或管理页 `/new/product`）：产品是实体卡的锚点
   （武将版本可以关联产品，也可以先留空）。
2. **再建卡牌定义 + 实体卡印刷**：按牌堆清点，逐张登记花色点数与角标。
3. **重复登记检查**：跑 `validate_data.py`，同一产品同花色点数的告警会列出来。

登记时的三条纪律：

- 卡面文字逐字抄进 `printed_text`，**不要改写原文**（检索正文由构建时生成，不用手填）；
- 只有亲眼核过实体卡，才把 `verification_status` 改成 `physical_verified`；
- 拿不准就留 `unverified`，需要人工对着卡图复核的异常写进 `review_note`。

## 2. 核验状态的用法

| 值 | 什么时候用 |
| --- | --- |
| `unverified` | 只是抄来/听说，还没核 |
| `community` | 来自社区资料库或 wiki，多条互相印证 |
| `official` | 有官方规则 / 官方 FAQ / 官方公告依据 |
| `physical_verified` | 已照着本人实体卡逐字核对 |
| `conflict` | 两个来源说法不一致，**两边都保留**；有 `review_note` 的实体写进 `review_note`，其余（FAQ / Keyword / Timing）写进正文 |

```yaml
source:
  type: discussion
  reference: ""
verification_status: conflict
review_note: >
  来源 A（社区资料库）说“可以重复触发”；来源 B（官方 FAQ 第 N 版）说“每回合一次”。
  两边都保留，待按实体卡与最新官方规则核验。
```

（`source` 只有 `type` + `reference`，展示名由 `reference` 推导。）

## 3. 常见报错与处理

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `未知字段（YAML 里多了这个字段？）` | 字段名写错或拼错（例如写了 `version:`），或用了已删除的旧字段 | 改成模型里存在的字段；来源信息放 `source`（只有 `type` / `reference`） |
| `id 重复：xxx 已在 data/... 中定义` | 两个文件用了同一个 id | 改 id（同名不同版本要用不同 id） |
| `引用的 技能(xxx) 不存在` | id 写错或目标文件还没建 | 建好目标文件，或修正引用 |
| `引用的 实体产品(card_demo) 不存在：…但同名的 卡牌定义(card_demo) 存在` | 引用到了另一种类型的 id | 字段写错了，改成对应类型的 id |
| `空引用：该字段不能是空字符串` | 写了 `product_id: ""` | 填有效 id，或改为 `null` |
| `自引用` | 列表里包含了自己的 id | 删掉 |
| `版本链成环` | `previous_version_id` 互相指向 | 改成单向链条 |
| `绑定的技能版本 … 与这里的技能 … 不一致` | `skills[].version_id` 指到了别的技能的版本 | 改成同一个 `skill_id` 下的版本，或清空让它按 `mode` 回退 |
| `该武将版本的技能没有可用文本` | `version_id` 为空，且该技能在该模式下没有任何 SkillVersion | 补一条 SkillVersion，或显式绑定 `version_id` |
| `武将牌系列 "…" 不在所属产品 … 的 general_sets 里` | `general_set` 与本产品的候选系列对不上 | 改成 `Product.general_sets` 里的值，或给产品补上这个系列 |
| `没有任何 SkillVersion` | 技能本体孤立 / 武将技能在该模式下没有版本 | 补 SkillVersion，或修正武将 `mode` |

`--strict` 会把警告也当成失败，适合在提交前跑一次。

## 4. 新增/修改时的自检清单

- [ ] 新记录有唯一的 `id`，并且符合前缀约定；
- [ ] 规则类实体（SkillVersion / FAQ / Keyword / Timing）填了 `source`，且 `verification_status` 与实际情况相符；
- [ ] 实体卡类实体的 `product_id` 指向正确的产品；
- [ ] 技能文本一字不改地来自实体卡/官方资料；
- [ ] 引用的 id 全部存在（校验器会替你查）；
- [ ] 修改技能时是**新增版本**而不是改旧文件；
- [ ] 跑过 `validate → build_db → pytest`；
- [ ] `git add` 里没有 `build/sgs.db`。

## 5. 想扩展枚举或数据模型

| 想做什么 | 改哪里 |
| --- | --- |
| 加一个游戏模式（例如“界将”） | `sgs_helper/constants.py` 的 `Mode` / `MODE_LABELS` / `MODE_ORDER` |
| 加一个来源类型 | `constants.py` 的 `SourceType` / `SOURCE_LABELS` |
| 给某类实体加字段 | `schema.py` 对应模型 + `db.py` 的 `COLUMNS`（多值字段记得加进 `JSON_COLUMNS`） |
| 加一种全新实体类型 | `constants.py` 的 `KINDS` + `schema.py` 模型 + `db.py` 的 `COLUMNS`/`INDEXES` + `validate.py` 的引用表 |
| 改表结构 | 同时把 `db.py` 的 `SCHEMA_VERSION` 加一，然后重新构建并重跑测试 |

改完模型/表结构后必须重跑测试（`tests/test_build.py` 会校验表结构集合与数量）。

## 6. 备份与恢复

- **权威数据**：`data/` 目录 + Git 历史。定期 `git push` 就是备份。
- **数据库**：不用备份，`python scripts/build_db.py` 随时重建。
- 想在本地留一份快照：`cp build/sgs.db /path/to/sgs-$(date +%F).db`（不要提交进 Git）。

## 7. 数据量变大时

现在是小数据量设计（全量重建耗时不到一秒）。若以后 YAML 上千条：

1. 先只做增量重建优化（只重建变化的表）——仍然保持“可全量重建”的能力；
2. 再考虑把 `search_index.body` 拆成独立全文表 / 引入 FTS5 中文分词扩展；
3. 不要把 SQLite 换成云数据库，除非出现多端并发写入的真实需求。
