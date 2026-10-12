# 国战图鉴

群狼环鼎国战资料的纯前端展示页。`main`只维护网页：无需Python、SQLite、后端、登录或在线接口。

## 使用

用浏览器打开根目录`index.html`，或把以下内容整体放入静态网站/页面资源包，保持相对路径：

```text
index.html
web/
  app.js              页面交互
  model.js            搜索、排序与关联
  style.css           手机与桌面布局
  catalog.js          页面实际加载的静态规则数据
  catalog.json        同内容JSON，方便复核
  favicon.svg
  assets/
    cards/            当前页面引用的277张卡图
    factions/         六张势力图
```

支持武将、技能、游戏牌、指示物、关键词与FAQ；手机和桌面自动适配。提供势力/卡牌类型筛选、正文联动、君主指示物说明、红黑花色图标及阅读位置恢复。牌堆信息暂不显示。

运行资源全部本地加载，无CDN、在线字体或API。页面有自己的静态规则数据包，这是查阅内容，不是数据库服务。

## 数据和历史工具

网页不需要YAML、SQLite生成器或历史编辑页，这些内容已从main移除。清理前的完整资料和维护工具保存在[资料维护分支](https://github.com/fengwang324/sgs-helper/tree/codex/data-maintenance)，快照提交为`4c89a84`。旧编辑页保留在历史中，当前网页没有编辑入口。

需要更新资料时，在维护分支修改和校验权威数据、导出新的`catalog.json`/`catalog.js`，再把两份静态数据包及新增卡图同步到main。图像文件名与数据一致，main的图片目录为`web/assets/cards/`；不要整分支合并而把已移除的工具带回main。直接复核JSON后也须同步页面使用的catalog.js。

## 前端检查（可选）

网页运行不需要Node；开发时可用Node执行`npm test`。测试无外部依赖，检查资料绑定、排序、五位君主指示物联动、JSON/JS一致及所有本地资源完整。

更新记录见[CHANGELOG.md](CHANGELOG.md)。
