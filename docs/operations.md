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
   **游戏牌拓展包**（`card_sets`）。
2. 再录武将：`/new/general` —— **只有名字必填**。
   - 「编号」是卡面编号（`general_number`，如 `WEI001`），内部 ID 留空会自动生成；
   - 技能行里搜索已有技能 → 选择这个武将用的**版本**（不选则按 `mode` 匹配该技能唯一一条版本）；
   - 没有的技能点 `+ 新建技能「xxx」`，填名称（只建技能本体）或再加描述（同时建技能版本）；
   - 需要改技能文本时用「+ 为这个技能新增版本」，旧版本永远保留。
3. 同一盒卡连续录：用「保存并新增下一位武将」（可继承产品信息），
   或详情页的「以此武将为模板新增」。

## 1. 现在的数据构成

技能版本来自
[《三国杀官方规则集 3.0》网页版](https://gltjk.com/sanguosha/rules/)，
FAQ 与规则关键词来自《群狼环鼎规则资料包 v1.0》的人工核对稿，
武将 / 卡牌侧来自群狼环鼎（`qlhd`）实体卡照片：

| 目录 | 现状 | 来源 |
| --- | --- | --- |
| `data/skills/`（291）、`data/skill_versions/`（291） | 技能本体与文本；SkillVersion 字段为 `id / skill_id / mode / product_id / text` | 群狼环鼎卡面 OCR + 人工核对录入；SkillVersion 不存来源元数据 |
| `data/faq/`（87） | 真实裁定，`source` 是多行文本 | 《群狼环鼎规则资料包 v1.0》核对稿（手册照片 + 实卡照片 + 通用规则网页 + B 站 UP 主） |
| `data/keywords/`（117） | 真实定义（只有 id / name / definition / variant） | 同上（关键词核对版，依据是手册页码，不录来源字段） |
| `data/generals/`（163）、`data/general_variants/`（163） | 真实数据，全是国战版本；同名但编号或称号不同的武将独立保留 | 群狼环鼎卡面 OCR + [三国杀WIKI（bilibili）](https://wiki.biligame.com/sgs/) |
| `data/products/`（1）、`data/cards/`（114） | 产品 `qlhd`，81 张游戏牌与 33 张指示物 / 配件牌 | 群狼环鼎实体卡，卡牌为一层 `Card` |

当前 `data/` 共 **1,227 条 YAML 实体**（schema 7；FAQ 87 条）；严格校验、SQLite 重建与测试结果由本轮验证记录填写。
163 条武将个体及 163 条玩法记录均保留；卡面编号不同或称号不同的武将分别记录，不按同名合并。
旧 OCR 技能清理与其他历史数据处理见对应更新日志。

重新导入 / 增量导入（站点更新时）：

```bash
pip install pypinyin                                    # 可选：生成拼音 id
bash scripts/import_rules_book.sh                       # 下载页面到 /tmp/rules
python scripts/import_rules_book.py --stage all         # 写 data/（会覆盖同名 id）
python scripts/validate_data.py && python scripts/build_db.py && python -m pytest -q
```

导入规则文本时要区分来源状态并按实际依据核对；`SkillVersion` 不保存 source 字段，不能声称某条文本已有结构化来源记录。
**核验一条就核对一条**：对着实体卡逐字比对，需要记差异就写进相应 FAQ 答案或规则正文。

## 1.1 武将是两层：General + GeneralVariant

武将数据由脚本 `scripts/import_generals_wiki.py` 从 WIKI 导入（只取「经典版本」段 = 标准身份版）。

- `General`（`data/generals/`）：只有 `id` / `name`。
  官方武将名不同就是不同的武将个体（诸葛亮 / 界诸葛亮 / 神诸葛亮）。
- `GeneralVariant`（`data/general_variants/`）：保存模式、势力、体力、性别、称号、编号、武将牌系列、产品和技能；没有 `variant` 标签。
  编号不同或称号不同的武将保留为不同 General，即使名字相同也不合并；确认是同一身份在不同模式下的记录时，才共用 General。
  来源不在这里存：由 `product_id` 定位到具体产品。
- 体力口径：身份局标准版记正常体力（3/4/5）；国战按阴阳鱼记 1.5 / 2 / 2.5
  （**不要**把半阴阳鱼记成 3/4/5）。
- `general_set`：只写一个具体系列名，候选来自所属产品的 `Product.general_sets`
  （没有独立的「系列」实体）；`skills[].version_id` 为空时按该武将版本的 `mode`
  匹配该技能唯一一条版本。
- 导入时**只录明确属于标准版的部分**，拿不准的不猜：
  萌娘百科的「标准版」名单与 WIKI 的 `武将包` 不一致的（例如袁术，WIKI 归在神话再临·雷包），
  先不录，留待按实体卡确认。

## 1.2 再录一批游戏牌（产品 / 卡牌）

1. **先建产品**（`data/products/` 或管理页 `/new/product`）：产品的 `card_sets`
   是游戏牌拓展包的候选全集（九变篇 / 势备篇 / 不臣篇 / 君主游戏牌 / 阵 / 君临天下·权…）。
   注意「指示物」是卡牌分类，不是拓展包。
2. **再建卡牌**（一层 `Card`）：按卡牌类别选模板：`card_basic.yaml`、`card_non_delayed_trick.yaml`、`card_delayed_trick.yaml`、`card_weapon.yaml`、`card_armor.yaml`、`card_treasure.yaml`、`card_mount.yaml`；指示物用 `marker.yaml`。
   游戏牌记录文本 `deck_info`（未知时留空）、纯文本 `effect`（无效果时省略）、可选外层 `note`；基本牌 / 锦囊牌的使用方式写在 `rules.use_methods`，武器攻击范围写外层 `attack_range`。分类自由填写（只有一级必填，二 / 三级可留空）。Card 不保存技能关联。
3. **归属检查**：跑 `validate_data.py`，卡牌 `card_set` 没有被任何产品声明、卡图文件不存在
   会告警；把「指示物」写进 `card_set` 会得到专门提示。分类取值本身不产生任何告警。

登记纪律：

- 卡面效果逐字抄进文本 `effect`，**不要改写原文**；无独立效果时省略。Card 不含 `effect.skills`；技能关系只在 GeneralVariant 维护；
- 只有亲眼核过实体卡才敢下结论；拿不准的就别写死，需要人工对着卡图复核的异常
  记在 Git 提交信息里。

## 2. 来源不一致怎么记

两个来源说法不一致时**两边都保留**，不要删掉任何一边：
把“来源 A（社区资料库）说……；来源 B（官方 FAQ 第 N 版）说……”写进实体自己的正文
（FAQ 写进 `answer`，技能写进 SkillVersion 的 `text`，关键词写进 `definition`；
卡牌这种正文必须逐字照抄卡面的，差异记在 Git 提交信息里），以后按实体卡与最新官方规则核验。

（SkillVersion 不保存 `source`；FAQ 的 `source` 是多行文本，原样展示。）

## 3. 常见报错与处理

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `未知字段（YAML 里多了这个字段？）` | 字段名写错，或用了已删除字段（如 SkillVersion 的 `source` / `version_label` / `previous_version_id`、GeneralVariant 的 `variant`、Card 的 `effect.skills` 或规则备注字段） | 依 schema 7 模型修正；FAQ 来源仍写在多行文本 `source`；游戏牌用 `category_1/2/3`、`deck_info` 与外层卡牌字段 |
| `id 重复：xxx 已在 data/... 中定义` | 两个文件用了同一个 id | 改 id；武将卡面编号或称号不同应保留为独立个体，不按同名合并 |
| `引用的 技能(xxx) 不存在` | id 写错或目标文件还没建 | 建好目标文件，或修正引用 |
| `引用的 实体产品(card_demo) 不存在：…但同名的 卡牌(card_demo) 存在` | 引用到了另一种类型的 id | 字段写错了，改成对应类型的 id |
| `空引用：该字段不能是空字符串` | 写了 `product_id: ""` | 填有效 id，或改为 `null` |
| `自引用` | 列表里包含了自己的 id | 删掉 |
| `绑定的技能版本 … 与这里的技能 … 不一致` | `skills[].version_id` 指到了别的技能的版本 | 改成同一个 `skill_id` 下的版本，或清空让它按 `mode` 回退 |
| `装备牌填写 rules` | 装备牌的能力与攻击范围使用外层字段 | 删除 `rules`；武器攻击范围写 `attack_range`，坐骑距离写在 `effect` 中 |
| `拓展包 "…" 不在任何产品声明的 card_sets（…）里` | 游戏牌 `card_set` 没有被任何产品声明 | 改成产品 `card_sets` 里的值，或给对应产品补上这个拓展包 |
| `"指示物" 不是游戏牌拓展包，而是卡牌分类` | 把「指示物」写进了卡牌 `card_set` | 指示物不填 `card_set`；把类型写在 `category_1` |
| `卡图 "…" 不存在：data/card_images/ 下没有这个文件` | `image` 文件名写错，或文件没放进来 | 放进 `data/card_images/`，或改成正确文件名（待补卡图就留空） |
| `该武将版本的技能没有可用文本` | `version_id` 为空，且该技能在该模式下没有任何 SkillVersion | 补一条 SkillVersion，或显式绑定 `version_id` |
| `武将牌系列 "…" 不在所属产品 … 的 general_sets 里` | `general_set` 与本产品的候选系列对不上 | 改成 `Product.general_sets` 里的值，或给产品补上这个系列 |
| `没有任何 SkillVersion` | 技能本体孤立 / 武将技能在该模式下没有版本 | 补 SkillVersion，或修正武将 `mode` |

`--strict` 会把警告也当成失败，适合在提交前跑一次。

## 4. 新增/修改时的自检清单

- [ ] 新记录有唯一的 `id`，并且符合前缀约定；
- [ ] `SkillVersion` 仅填 `id / skill_id / mode / product_id / text`；FAQ 有来源时填文本 `source`；
- [ ] 武将玩法记录的 `product_id` 指向正确的产品；游戏牌的 `card_set` 是某个产品声明过的拓展包（或留空）；指示物不填 `card_set`；
- [ ] 游戏牌具有 `deck_info` 文本字段（未知内容留空），基本牌三级分类只写牌族，`image` 指向的文件确实在 `data/card_images/` 下；卡牌效果是纯文本；武器 `attack_range` 与可选 `note` 在 Card 外层，装备牌不写 `rules`；
- [ ] 技能文本一字不改地来自实体卡/官方资料；
- [ ] 引用的 id 全部存在（校验器会替你查）；
- [ ] 修改技能文本时新增 SkillVersion，按需更新 GeneralVariant 的 `skills[].version_id`；模型不存版本标签或前序链；
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
