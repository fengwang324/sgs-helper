# 路线图（当前阶段之后做什么）

原则：**先把资料库本身做对**，再谈前端与智能问答。任何新功能都不应破坏
“YAML 权威源 → SQLite 生成物”的结构。

## 阶段 0（当前）：资料库底座 ✅

- [x] YAML 数据模型（10 类实体，技能/卡牌/武将两层，版本不覆盖）
- [x] Pydantic 校验 + 交叉引用 + 一致性检查（错误 / 警告分级）
- [x] SQLite 全量重建（幂等、可追溯、FTS5 可选）
- [x] 查询工具（skill / general / card / faq / keyword / product / search，支持 `--json`）
- [x] pytest 测试（328 个）
- [x] 示例占位数据

## 阶段 1：换成真实资料（最高优先级）

目标：让库里的内容真正可用。

1. 按 `docs/operations.md` 的顺序，用实体产品 + 官方资料替换 SAMPLE 数据；
2. 每核对一条就留下痕迹：对不上的写进实体正文，或在 Git 提交信息里说明；
3. 遇到来源冲突就把两边说法都写进正文，不要删数据；
4. 补充本人常打的国战武将包与身份局常用武将（先窄后广，别追求覆盖全部）。

验收：`validate_data.py` 零错误；能用 `query.py` 回答“这个技能现在怎么写的”“这张卡的角标是什么”。

## 阶段 2：只读 HTTP API（FastAPI）

目标：给微信小程序提供数据接口。当前**不需要**用户系统。

```text
微信小程序  →  FastAPI（只读）  →  build/sgs.db
```

实现建议：

- 新增 `api/` 目录（或 `sgs_helper/api.py`），用 FastAPI 包一层 `sgs_helper/repo.py`：
  ```python
  @app.get("/api/skills/{skill_id}")
  def get_skill(skill_id: str, as_of: date | None = None):
      conn = get_conn()                       # 单进程只读连接
      report = skill_report(conn, skill_id, as_of)
      if report is None:
          raise HTTPException(404, "skill not found")
      return report                           # 已经是纯 dict，可直接序列化
  ```
- 需要的接口与函数映射见 README 的表格；
- 只读、无写入、无鉴权；启动时若 `build/sgs.db` 不存在或 `meta.yaml_manifest_sha256`
  与实际 YAML 不一致，直接启动失败（避免提供过期数据）；
- 部署方式保持简单：`uvicorn` 单进程 + systemd/Nginx（不要上 K8s）。

验收：小程序能搜到武将/技能/卡牌/FAQ；`GET /api/skills/{id}/versions` 能返回完整版本历史。

## 阶段 3：微信小程序（最小可用）

- 页面：搜索、武将详情（技能全文）、卡牌详情（含各产品印刷与角标）、FAQ 速查、关键词速查；
- 不做：登录、收藏云同步、社区、排行榜；
- 数据全部来自阶段 2 的只读接口，不直连数据库。

## 阶段 4：基于资料库的 AI QA

前提：**资料库本身可信**（阶段 1 完成），否则问答只会放大错误。

建议路线（简单优先）：

1. 先用“检索 + 拼接引用”的方式：`search()` 找候选 → 把技能文本/FAQ/关键词原文喂给模型 →
   要求回答**必须给出 `entity_id` 与 `yaml_file:line`**，便于人工复核；
2. 检索层就用现有 SQLite（必要时把 `search_index` 升级成分词表），不要一上来引入向量库；
3. 明确区分：正文里写明“待核验”或“两边说法不一致”的内容，回答时必须标注出来；
4. 模型只允许引用库里已有的原文，不允许自行生成规则；
5. 有真实需求后，再考虑 RAG（embedding + 重排），并保留“答案必须可溯源到 id”这条底线。

## 明确不做

微信登录与用户体系、管理后台、MySQL/PostgreSQL、Docker 集群 / Kubernetes、微服务、
Elasticsearch、自动抓取全网、完整规则模拟器。
（“本地资料维护网页” `sgs_helper/admin/` 已经做了，但它只是本机上直接编辑 YAML 的工具，
不属于这里的线上后台。）
这些都等出现**真实需求**再说；本项目的数据结构不会阻碍它们以后加入。
