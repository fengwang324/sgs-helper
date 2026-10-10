# 游戏牌规则与指示物

游戏牌与指示物继续共用 `Card`、`data/cards/` 和 ID 引用机制。游戏牌使用 `deck_info` 记录已知的实物牌堆组成；没有录入的牌堆信息写空字符串，不根据常识补猜。指示物不填写 `card_set`、`rules` 或 `deck_info`。

## 字段

| 类型 | 专用字段 | 共用内容 |
| --- | --- | --- |
| 游戏牌 | `deck_info` | `effect` 为卡面效果文本；可选 `card_set` |
| 基本牌 / 锦囊牌 | `rules.use_methods`（0～N 种使用方法） | 可选 `label`、`timing`、`target`、`effect` |
| 武器 | `attack_range` | `effect` 效果文本 |
| 防具 / 宝物 / 坐骑 | 无 | `effect` 效果文本 |
| 指示物 | 无 | 自由分类与 `effect` 文本 |

卡牌 `effect` 是纯文本；没有独立效果时省略该字段。基本牌 / 锦囊牌规则只使用 `rules.use_methods`，方法的 `label` 用于“重铸”等具名方法。武器攻击范围放在外层 `attack_range`；坐骑距离效果写在效果文本中。空字段省略。

```yaml
# 结构示例；占位文字不是已核对规则。
id: card_change_me
name: 卡牌名
category_1: 游戏牌
category_2: 锦囊牌
category_3: 非延时锦囊
deck_info: ''
rules:
  use_methods:
    - timing: 出牌阶段。
      target: 一名其他角色。
      effect: 按已核对规则填写。
      # label: 重铸
```

## 数据迁移说明

2026-10-10 将 81 张游戏牌补齐 `deck_info` 字段；原来未录入的牌堆资料保留为空，杀的已录入花色点数移入该字段。33 个非游戏牌条目（含指示物及其他自由分类卡牌）不增加 `deck_info`。卡面效果对象改为文本，空效果省略；技能关联不再放在 `effect`。

旧规则备注中，酒的两条规则说明迁至 FAQ；联军盛宴方法备注与已有完整效果重复，清除重复备注。迁移保留 ID、卡面文字、规则机制和原有牌堆点数信息，不代表依据规则书重新核对。

模板按类别拆分，见 `data/_templates/card_basic.yaml`、`card_non_delayed_trick.yaml`、`card_delayed_trick.yaml`、`card_weapon.yaml`、`card_armor.yaml`、`card_treasure.yaml`、`card_mount.yaml` 与 `marker.yaml`。
