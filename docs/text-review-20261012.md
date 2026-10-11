# 2026-10-12 全文核对记录

本轮通读291段技能正文、43项关键词、25项FAQ及114张卡牌文本；对疑似漏字、漏花色、变量、断句及OCR尾文使用本地卡图核对。618条武将、玩法记录、技能名与产品元数据检查了编码、空白和字段保留。用户已核对的称号、姓名及技能本体名称未修改。不是全库逐图OCR，也不以其他版本规则替换当前资料。

47段技能正文进行了修正。逐条修改前后文本与图证清单保存在仓库外清洗记录中，Git差异为仓内可复原依据。

| 技能版本 | 核对原图 |
| --- | --- |
| `skill_bei_ge_guozhan_qlhd_v1` | `g_qun_012.jpg` |
| `skill_bu_qu_guozhan_qlhd_v1` | `g_wu_013.jpg` |
| `skill_gui_cai_guozhan_qlhd_v1` | `g_wei_002.jpg` |
| `skill_feng_yang_guozhan_qlhd_v1` | `g_wu_086.jpg` |
| `skill_hun_shang_guozhan_qlhd_v1` | `g_wu_010.jpg` |
| `skill_ji_li_2_guozhan_qlhd_v1` | `g_shu_020.jpg` |
| `skill_jie_ming_guozhan_qlhd_v1` | `g_wei_013.jpg` |
| `skill_tian_yi_guozhan_qlhd_v1` | `g_wu_012.jpg` |
| `skill_wen_ji_guozhan_qlhd_v1` | `g_qun_shu_072.jpg` |
| `skill_ying_zi_guozhan_qlhd_v1` | `g_wu_005.jpg` |
| `skill_zhu_wei_guozhan_qlhd_v1` | `g_wu_019.jpg` |
| `skill_xun_ji_guozhan_qlhd_v1` | `g_jin_037.jpg` |
| `skill_fan_kui_guozhan_qlhd_v1` | `g_wei_002.jpg` |
| `skill_kuang_fu_guozhan_qlhd_v1` | `g_qun_017.jpg` |
| `skill_liu_li_guozhan_qlhd_v1` | `g_wu_006.jpg` |
| `skill_qi_ao_guozhan_qlhd_v1` | `g_shu_078.jpg` |
| `skill_qing_cheng_guozhan_qlhd_v1` | `g_qun_018.jpg` |
| `skill_xuan_lve_guozhan_qlhd_v1` | `g_wu_021.jpg` |
| `skill_zhuo_sheng_guozhan_qlhd_v1` | `g_jin_016.jpg` |
| `skill_nei_ji_guozhan_qlhd_v1` | `g_jin_039.jpg` |
| `skill_jue_jian_guozhan_qlhd_v1` | `g_jin_041.jpg` |
| `skill_mi_dao_guozhan_qlhd_v1` | `g_qun_wei_048.jpg` |
| `skill_shu_shen_guozhan_qlhd_v1` | `g_shu_016.jpg` |
| `skill_zhi_dao_guozhan_qlhd_v1` | `g_qun_050.jpg` |
| `skill_zong_yu_guozhan_qlhd_v1` | `g_em_004.jpg` |
| `skill_tong_ling_guozhan_qlhd_v1` | `g_shu_qun_072.jpg` |
| `skill_xun_xun_guozhan_qlhd_v1` | `g_wei_017.jpg` |
| `skill_zhen_te_guozhan_qlhd_v1` | `g_wu_078.jpg` |
| `skill_jiang_lve_guozhan_qlhd_v1` | `g_shu_023.jpg` |
| `skill_san_chen_guozhan_qlhd_v1` | `g_jin_019.jpg` |
| `skill_qiu_an_guozhan_qlhd_v1` | `g_wei_shu_079.jpg` |
| `skill_bing_xin_guozhan_qlhd_v1` | `g_jin_032.jpg` |
| `skill_huai_yi_guozhan_qlhd_v1` | `g_am_004.jpg` |
| `skill_fang_zhu_guozhan_qlhd_v1` | `g_wei_014.jpg` |
| `skill_bao_lie_guozhan_qlhd_v1` | `g_shu_wei_041.jpg` |
| `skill_feng_lve_guozhan_qlhd_v1` | `g_qun_078.jpg` |
| `skill_guo_wu_guozhan_qlhd_v1` | `g_qun_089.jpg` |
| `skill_ying_hun_guozhan_qlhd_v1` | `g_wu_009.jpg` |
| `skill_wei_meng_guozhan_qlhd_v1` | `g_shu_073.jpg` |
| `skill_bo_yan_guozhan_qlhd_v1` | `g_wu_089.jpg` |
| `skill_kuang_cai_guozhan_qlhd_v1` | `g_qun_060.jpg` |
| `skill_wu_sheng_guozhan_qlhd_v1` | `g_shu_002.jpg` |
| `skill_wu_shuang_guozhan_qlhd_v1` | `g_qun_002.jpg` |
| `skill_zhi_wei_guozhan_qlhd_v1` | `g_wu_078.jpg` |
| `skill_ao_cai_guozhan_qlhd_v1` | `g_wu_042.jpg` |
| `skill_he_yi_guozhan_qlhd_v1` | `g_wei_018.jpg` |
| `skill_ji_zhao_guozhan_qlhd_v1` | `g_em_001.jpg` |

卡牌补齐29处时机、26处目标字段的句末句号；敕令、联军盛宴、铁索连环规范末尾括号和句号。诏书的引文与署名单独成行，署名不补句号。FAQ正文和关键词定义未发现缺少句末标点；队列、阵法技按已确认文案修改。

四张锦囊的通用重铸规则改为外层 `recast: true` 与重铸关键词：分道扬镳、勠力同心、铁索连环、知己知彼。保留其正常使用规则及技能中自行定义的重铸效果。

逐条快照比较确认1091个实体ID、全部武将身份、称号、技能名称、图片引用及293条绑定均保留；本轮数据变动限定于47个技能的正文、29张卡的规则/效果/重铸标记、2个关键词定义。

两个原图差异保留供后续核对：盗书的绑定卡图与已录入的技能正文版本不一致；知己知彼卡面简述目标与现有结构化目标条件不同。本轮保留现有规则，不自行改变版本或合法目标范围。正文引用但未独立收录的名称也保留原文，不补造规则或恢复此前删掉的词条。

验证：严格模型校验0错误/0警告；SQLite schema 8、1091条实体，完整性ok，外键异常0；数据库及读写保护pytest 221项通过，前端Node行为检查14项通过，另进行了实际页面点击验收。
