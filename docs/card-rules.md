# 游戏牌规则与指示物

游戏牌与指示物继续共用 `Card`、`data/cards/` 和 ID 引用机制。两者在内容上分开：
游戏牌增加可选的 `rules`；指示物（以及其他自由一级分类）只使用原有 `effect`，不接受 `rules`。
分类仍是自由文本，游戏牌类别未确定或暂缺资料时可以不填写具体规则。

## 字段

所有规则标量都是文本，允许 `X`、条件句、换行和空字符串，不做数值或固定枚举校验。

| 类型 | 专用字段 | 共用内容 |
| --- | --- | --- |
| 基本牌 / 锦囊牌 | `rules.use_methods`（0～N 种使用方法） | `effect.text` 原文、`effect.skills` 技能引用、`rules.note` |
| 武器 | `rules.attack_range` 攻击范围 | `effect.text` 效果、`effect.skills` 技能引用、`rules.note` |
| 防具 | 无 | 同上 |
| 坐骑 | `rules.distance_modifier` 距离修正 | 同上 |
| 宝物 | 无 | 同上 |
| 指示物 | 不填写 `rules` | 自由 `effect.text` 与 `effect.skills` |

`effect.text` 是卡面原文，`use_methods` 是按规则书组织的使用说明。两种来源分别维护；
自动生成界面样式或拆分结构不应改写原文。装备能力已有独立技能记录时继续引用，
不重复复制 SkillVersion 文本。装备牌通用使用规则由关键词维护，特殊例外写在具体卡的效果或 note。

每种使用方法包含 `label`（可空的方法名称）、`timing`（出牌时机）、`target`（使用目标）、
`effect`（作用效果）、`note`（该方法的补充说明），字段均可空。
卡级 `rules.note` 与方法级 `note` 分别存整张卡和某种使用方法的说明；独立问答继续放 FAQ。
坐骑距离修正应写清计算方向；含 X 时在效果或 note 中说明 X 的含义。

```yaml
# 结构示例；占位文字不是已核对规则。
rules:
  use_methods:
    - label: 使用方法一
      timing: 按当前规则书填写
      target: 按当前规则书填写
      effect: 按当前规则书填写
      note: ''
    - label: 使用方法二
      timing: ''
      target: ''
      effect: ''
      note: ''
  note: ''
```

武器只增加 `rules: {attack_range: 'X', note: ''}`；坐骑只增加
`rules: {distance_modifier: '按卡面填写并明确方向', note: ''}`；防具、宝物只增加 `rules: {note: ''}`。
模板见 `data/_templates/card.yaml` 和 `data/_templates/marker.yaml`。

## 迁移与维护

`scripts/migrate_card_rules.py` 默认输出计划，`--write` 才写入；已有 rules 的记录跳过。
本轮迁移 81 张游戏牌，保留 ID、分类、图片、拓展包、完整原文和技能引用；指示物文件保持原样。
只拆原文中明确的“出牌阶段，对…使用。”句式，提取原文已有的攻击范围和纯 `+1/-1` 距离修正。
不能明确拆分时保留全文；基本牌原本缺失的规则、武器原本缺失的范围、特殊坐骑原本缺失的修正
均留空，后续按所用规则书核对。迁移不表示这些规则已与规则书复核。

录入页提供多种使用方法的添加 / 删除及文本输入，失败回显与草稿恢复保留方法内容；
分类变化时已有专用内容仍显示。改为指示物前须清空 rules，避免静默丢失原有规则。
普通分类名不需登记到 constants，基本牌 / 锦囊牌 / 装备牌只是界面提供对应输入项的约定。

SQLite schema 6 在 `cards` 中新增 `rules TEXT`（JSON 对象；指示物为 SQL NULL）。
查询层解码为对象，新字段参与全文检索和正文实体链接；管理详情、Wiki 和 CLI 同步展示。
更改 Python 模型后，已有本地管理进程需重新加载代码。最终纯前端可以直接读取同一规则对象。
