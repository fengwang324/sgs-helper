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
   - 技能行里搜索已有技能 → 选择这个武将用的**版本**（不选则自动推导）；
   - 没有的技能点 `+ 新建技能「xxx」`，填名称（只建技能本体）或再加描述（同时建技能版本）；
   - 需要改技能文本时用「+ 为这个技能新增版本」，旧版本永远保留。
3. 同一盒卡连续录：用「保存并新增下一位武将」（可继承产品信息），
   或详情页的「以此武将为模板新增」。
4. 录完随时看 `/review`，把 `unverified` / `community` / `conflict` 的记录逐条核验掉。

## 1. 把 SAMPLE 数据换成真实数据

当前 `data/` 里全是占位数据（`【SAMPLE】` / `【示例占位】`）。建议按下面的顺序替换：

1. **先建产品**（`data/products/`）：产品是其它记录的锚点，`product_id` 都要指向它。
2. **再建技能本体 + 技能版本**（`data/skills/`、`data/skill_versions/`）：文本一定要照抄实体卡/官方原文，不要改写。
3. **再建武将**（`data/generals/`）：确认体力、势力、技能组、所属产品。
4. **再建卡牌定义 + 实体卡印刷**（`data/cards/`、`data/card_printings/`）：按牌堆清点，逐张登记花色点数与角标。
5. **最后补 FAQ / 关键词 / 时点**。

替换时的三条纪律：

- 卡面文字逐字抄进 `printed_text`，需要检索用词就填 `normalized_text`，**不要改写原文**；
- 只有亲眼核过实体卡，才把 `verification_status` 改成 `physical_verified`，并把 `source.type` 改成 `physical_card`；
- 拿不准就留 `unverified`，并在 `notes` 写清“待核验什么”。

删掉示例文件时请连带删掉它的引用（引用不存在会被校验器报错，正好当检查清单用）。

## 2. 核验状态的用法

| 值 | 什么时候用 |
| --- | --- |
| `unverified` | 只是抄来/听说，还没核 |
| `community` | 来自社区资料库或 wiki，多条互相印证 |
| `official` | 有官方规则 / 官方 FAQ / 官方公告依据 |
| `physical_verified` | 已照着本人实体卡逐字核对 |
| `conflict` | 两个来源说法不一致，**两边都保留**，在 notes 写清各自说法与待办 |

```yaml
source:
  type: discussion
  title: 某玩家讨论帖
  reference: ""
  notes: 说法 A
verification_status: conflict
notes: >
  来源 A（社区资料库）说“可以重复触发”；来源 B（官方 FAQ 第 N 版）说“每回合一次”。
  两边都保留，待按实体卡与最新官方规则核验。
```

## 3. 常见报错与处理

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `未知字段（YAML 里多了这个字段？）` | 字段名写错或拼错（例如写了 `version:`） | 改成模型里存在的字段；产品信息用 `edition`/`series`，来源信息放 `source` |
| `id 重复：xxx 已在 data/... 中定义` | 两个文件用了同一个 id | 改 id（同名不同版本要用不同 id） |
| `引用的 技能(xxx) 不存在` | id 写错或目标文件还没建 | 建好目标文件，或修正引用 |
| `引用的 实体产品(card_demo) 不存在：…但同名的 卡牌定义(card_demo) 存在` | 引用到了另一种类型的 id | 字段写错了，改成对应类型的 id |
| `空引用：该字段不能是空字符串` | 写了 `product_id: ""` | 填有效 id，或改为 `null` |
| `自引用` | 列表里包含了自己的 id | 删掉 |
| `版本链成环` | `previous_version_id` 互相指向 | 改成单向链条 |
| `effective_to 早于 effective_from` | 区间写反 | 修正日期 |
| `生效区间重叠 …会出现歧义` | 同一技能同一模式有两段重叠的生效期 | 收紧旧版本 `effective_to`，或确认是否该当重复版本 |
| `时点 … 未反向包含` | 只写了一边的 prev/next | 两边都写（保持对称，校验器不强制但会提醒） |
| `没有任何 SkillVersion` | 技能本体孤立 / 武将技能在该模式下没有版本 | 补 SkillVersion，或修正武将 `mode` |

`--strict` 会把警告也当成失败，适合在提交前跑一次。

## 4. 新增/修改时的自检清单

- [ ] 新记录有唯一的 `id`，并且符合前缀约定；
- [ ] `source` 填了，且 `verification_status` 与实际情况相符；
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
