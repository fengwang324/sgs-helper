# 本地资料管理网页（`sgs_helper.admin`）

> 目标：**用浏览器维护资料，不必频繁手改 YAML**，同时保持
> `YAML 是唯一权威数据源，SQLite 只是生成物` 这条铁律不被破坏。
>
> 当前阶段只做“本地管理工具”，不是正式微信小程序，不引入 React/Vue。

技术栈：FastAPI + Jinja2 + 手写 HTML/CSS + 少量原生 JavaScript。
数据层、校验层、构建层全部复用现有 `sgs_helper` 代码，**没有另起一套规则**。

---

## 1. 启动

```bash
# 在容器里（已挂载 /data）
cd /data/wf/1002/sgs-helper

pip install -e ".[admin]"        # 一次性：fastapi / uvicorn / jinja2 / python-multipart

uvicorn sgs_helper.admin.app:app --host 127.0.0.1 --port 8000
# 或者
python scripts/admin.py --port 8000          # 等价，带更友好的启动提示
python scripts/admin.py --port 8000 --reload # 开发用
```

浏览器访问 <http://127.0.0.1:8000>。

可用环境变量（`scripts/admin.py` 会自动设置）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SGS_DATA_DIR` | `./data` 或仓库根的 `data/` | YAML 权威源目录 |
| `SGS_DB` | `./build/sgs.db` | SQLite 生成物路径 |

`--host 0.0.0.0` 可以把页面暴露到局域网；本工具没有登录与权限体系，
只在可信网络里这么用（表单带 CSRF token，但这不是认证）。

---

## 2. 页面入口

| 路由 | 作用 |
| --- | --- |
| `GET /` | 首页：统一搜索框、各类型条数、核验状态分布、SQLite 状态、当前校验错误与警告 |
| `GET /search?q=&kind=&status=&mode=` | 跨 9 类实体检索，显示类型 / 名称 / 模式 / 来源 / 核验状态 / YAML 位置 |
| `GET /list/{kind}?q=&mode=&status=&product=&package=&page=` | 分类列表 + 筛选 + 分页 |
| `GET /view/{kind}/{id}` | 详情页（技能/卡牌/武将有专门的分层展示） |
| `GET /new/{kind}` `POST /new/{kind}` | 新增（表单由 Pydantic 模型生成） |
| `GET /edit/{kind}/{id}` `POST /edit/{kind}/{id}` | 编辑 |
| `GET /delete/{kind}/{id}` `POST /delete/{kind}/{id}` | 删除确认 + 引用检查 |
| `GET /review[?status=]` | 待核验清单：`unverified` / `community` / `conflict` |
| `POST /rebuild` | 手动按当前 YAML 全量重建 SQLite |
| `GET /raw/{kind}/{id}` | 直接查看该记录所在 YAML 文件原文 |
| `GET /healthz` | JSON 健康检查（数据目录、校验结果、DB 是否过期、各类条数） |
| `GET /help` | 站内使用说明 |

`{kind}` 只接受 9 个值：`product` / `general` / `skill` / `skill_version` /
`card` / `card_printing` / `faq` / `keyword` / `timing`。

### 详情页看到什么

- **技能**：`SkillDefinition` → 每个模式下的**当前有效版本**（按 `mode + effective_from + effective_to` 推导）
  → 全部历史版本（状态、区间、前一版本、后继）→ 历史版本链 → 每个模式一个「新增技能版本」按钮。
- **卡牌**：`CardDefinition` → 所有 `CardPrinting`，逐个显示产品、花色、点数、右下角角标、
  卡牌编号、包/牌堆、数量、卡面文本（逐字）。
- **武将**：`General` → 所属产品 → 关联技能 → 每个技能在**该武将模式**下的当前有效 `SkillVersion` 文本，
  以及全部相关版本。
- **产品**：收录武将、该产品的实体卡、绑定该产品的技能版本。
- **所有类型**：完整字段表（引用字段是可点击链接）、`source.type/title/reference/notes`
  （`reference` 是 URL 时可直接点击）、反向引用清单、YAML 原文。

---

## 3. 写入流程（为什么不会把资料写坏）

```text
浏览器表单
   │ forms.parse_form()            字段级转换（多行列表、数字、日期、枚举、内嵌 source）
   ▼
候选实体 dict
   │ AdminStore.validate_candidate()
   │   在内存里 load_data_dir() → 用候选替换/追加 → validate_entities() 完整校验
   │   （模型字段 + 交叉引用 + 一致性 + 重复 id，与 CLI 校验同一套代码）
   ├── 校验失败 → 返回 400，展示 文件 / 字段 / 原因；**一个字都不写盘**
   ▼
校验通过
   │ yaml_writer.upsert_entity()   临时文件 + fsync + os.replace，原子替换
   ▼
写回 data/**/*.yaml（Git 工作区）
   │ build_database(data_dir, tmp) 先建临时库，成功后 os.replace 到 build/sgs.db
   ├── 意外失败 → 用备份内容回滚 YAML，旧数据库原样保留
   ▼
SQLite 更新完成，303 重定向到详情页
```

关键点：

1. **读也走 YAML**。列表、详情、搜索都直接读权威源（`validate_data_dir` 的 bundle），
   不使用可能过期的 `build/sgs.db`；搜索复用 `db._search_row` + `repo._score`，不重写匹配规则。
   页面顶部会在 SQLite 与 YAML 指纹不一致时给出「已过期」提示。
2. **SQLite 只由 `build_database()` 全量重建**，网页没有任何一处直接 `UPDATE/INSERT/DELETE` 数据库。
3. **先校验后落盘**，磁盘上不会出现半合法的 YAML；写盘后还会再校验一次（读盘结果）。
4. **并发保护**：编辑页会带上该记录的内容哈希（`_base_sha`），提交时对不上就拒绝写入并提示刷新
   （两个标签页同时改同一条、或你手工改过 YAML，都会命中）。
5. **原子写**：`临时文件 → fsync → os.replace`；重建数据库同样是 `临时库 → os.replace`。
6. **CSRF token**：所有写操作都要带页面里的随机 token，浏览器同源策略保证外部网页拿不到它。

写 YAML 的可读性保证：

- 字段顺序固定为模型字段顺序（不是字母序）；
- 规则文本类字段（`text` / `printed_text` / `question` / `answer` / `definition` / `description` /
  `change_note`）用块状标量 `|` / `|-`，**不会折行**（YAML 折行会把中文文本变成带空格的一行）；
- 文件顶部人工维护的注释块原样保留；
- 原文是「单条映射 / 顶层列表 / `items:` 包装」的哪种结构，写回就还是哪种结构。

---

## 4. 表单

字段由 `MODEL_BY_KIND[kind].model_json_schema()` 生成：

| 模型类型 | 控件 |
| --- | --- |
| 枚举（`mode` / `verification_status` / `source.type` / `suit` / `card_type` / `gender`） | 下拉框（中文标签） |
| `date` | `input[type=date]`，写 `YYYY-MM-DD` |
| `int` / `float` | 数字输入（`hp` 步进 0.5） |
| `list[str]` | 文本框，**一行一项**；留空表示空列表（不会用逗号切分，别名里可以带逗号） |
| 一般 `str` | 单行输入 |
| 长文本（`text` / `printed_text` / `notes` …） | 多行文本框 |
| 内嵌 `Source` | 一组固定四字段（`type` / `title` / `reference` / `notes`） |

引用型字段（`product_id` / `skill_id` / `skill_ids` / `previous_version_id` /
`related_*`）会带候选项：单值字段用 `datalist`，列表字段提供可点击的「+ 选项」按钮追加。
`source` 必填的类型（`skill_version` / `card_printing` / `faq` / `keyword` / `timing`）如果留空会直接报错。

修改 **ID** 会同时移动 YAML 文件；如果还有别的记录引用这个 ID，保存会被校验拦下（不会留下断链）。
ID 必须匹配 `^[a-z][a-z0-9_]*$`，非法 ID（例如 `../../etc/passwd`）在解析阶段就被拒绝。

---

## 5. 技能版本的正确用法（重点）

1. 打开 `<某个技能>` 详情页；
2. 在目标模式那一行点「新增技能版本」；
3. 表单会自动带入 `skill_id`、`previous_version_id`（该模式当前有效版本）、`mode`、`product_id`、`package`，
   并从被取代的版本复制 `source.*`，`effective_from` 默认今天；
4. 修改 `text` / `effective_from` / `change_note` / `source` / `verification_status`；
5. 保存 → 新增一条 `SkillVersion` 文件，**旧版本文件字节不变**（有测试专门盯着这一点）。

校验器会继续检查：`previous_version_id` 必须属于同一技能、不能成环、同一
`(skill_id, mode, 产品范围)` 内生效区间不应重叠（重叠只是警告，但推导“当前有效版本”会有歧义，
详情页会把并列版本标出来，绝不静默挑一个）。

---

## 6. 删除规则

删除前会用 `REFERENCE_FIELDS`（与校验器同一张声明表）反查引用：

- 有引用 → **拒绝删除**，页面列出引用方（类型 / 名称 / id / 字段）；
- 无引用 → 先做一次「删掉这条之后数据集是否仍然合法」的预校验，通过才删；
- 删除技能版本时，如果它还有后继版本（被 `previous_version_id` 引用）会被拒绝；
  它只是历史版本时可以删，删除确认页会说明它是不是「当前有效版本」。

另有一条测试保证：任何新增的 `*_id` / `*_ids` 模型字段都必须在 `REFERENCE_FIELDS` 里声明，
否则测试直接失败 —— 防止以后加了引用字段而删除保护静默失效。

---

## 7. 待核验流程

`/review` 按 `verification_status` 汇总：

- `unverified` 未核验；
- `community` 仅有社区资料；
- `conflict` 来源冲突。

逐条打开 → 对着实体卡核验 → 改 `verification_status`（通常是 `physical_verified`）、
把 `source.type` 换成 `physical_card` 并填 `source.title` / `source.reference`（页码、照片说明等）→ 保存。

---

## 8. 已知限制（有意为之）

1. **写 YAML 会规范化格式**：记录内部的行内注释、锚点/别名、原有引号风格不会保留
   （保留的是：字段值本身、字段顺序、文件顶部注释块、文件结构）。
   因此不建议在记录内部塞关键说明——说明写在 `notes` 字段或文件顶部注释里。
2. **一个文件多条记录**（例如 `data/keywords/core_keywords.yaml`）编辑其中一条会整文件重写；
   其他记录的**内容**不变，但格式也会被规范化一次。
3. **列表字段是“一行一项”文本**：空行会被忽略，前后空格会被去掉（字符串首尾空格本来也会被
   `str_strip_whitespace` 规范化）。
4. **没有登录/权限/审计**：本地工具，只服务于「一个人维护自己的资料库」。
5. **不做并发写入的合并**：同一记录被两处同时修改时，后提交的一方会被拒绝并要求刷新。
6. 不是正式小程序后端：没有分页 API 设计、限流、缓存策略；未来接小程序请从
   `sgs_helper/repo.py`（只读 SQLite）那一层开始，不要直接复用管理端的写接口。

---

## 9. 测试

```bash
python -m pytest tests/test_admin.py -q
```

覆盖：

- 页面能启动：9 类实体的列表/详情/新增页 + 编辑页 + 删除确认页 + 待核验 + 说明 + `/healthz`；
- 搜索可用：跨类型命中、按类型筛选、按核验状态筛选、按 id 搜索、结果里带模式/来源/核验状态；
- 编辑 YAML 后能重建数据库：写盘内容正确、`meta.yaml_manifest_sha256` 与数据目录一致、
  `repo.faq_report()` 读到新值、页面立即显示新值、多行文本写成块状标量、顶部注释保留；
- 非法修改被 validate 拦截：引用不存在的技能 → 400，页面给出文件/字段/原因，
  **YAML 字节不变、SQLite 字节不变**；重复 id、非法 id、必填 source 缺失同样被拦截且不落盘；
- SkillVersion 新增不覆盖旧版本：旧文件字节不变、新文件生成、数据库里两条并存、
  新模式下的当前有效版本正确、详情页显示版本链；
- 被引用实体不能删除：产品被武将/卡牌/技能版本引用 → 拒绝并列出引用方；
  有后继版本的技能版本 → 拒绝；无引用记录 → 正常删除（文件消失、数据库更新）；
- 回归：YAML 往返无损（含 `null/true/数字/日期/以 - : # 开头/多行/尾换行/中文标点`）、
  任意真实实体 `模型 → YAML → 模型` 完全一致、多实体文件不破坏其他记录、
  CSRF token 必需、并发修改被检测、`REFERENCE_FIELDS` 声明完整性。

所有测试都在 `tmp_path` 里的**副本**上跑，绝不修改仓库里真实的 `data/`。
