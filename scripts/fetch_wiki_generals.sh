#!/usr/bin/env bash
# 抓取三国杀 WIKI（bilibili）的武将页，供 scripts/import_generals_wiki.py 解析。
#
#   bash scripts/fetch_wiki_generals.sh [输出目录]      # 默认 /tmp/wiki/generals
#
# 只做“下载”：先取 Category:武将 的页面清单，再逐页取 action=parse 的
# wikitext（模板字段：势力/称号/体力/武将包…）+ text（渲染后的「经典版本」段：技能）。
#
# 站点对高频请求会返回 HTTP 567（反爬），所以：每页之间 sleep、失败重试 3 次，
# 重试仍失败时换一个 UA + Referer 再试一次（实测有效）。
set -euo pipefail

API="https://wiki.biligame.com/sgs/api.php"
OUT="${1:-/tmp/wiki/generals}"
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
UA2="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
REFERER="https://wiki.biligame.com/sgs/%E9%A6%96%E9%A1%B5"

mkdir -p "$OUT"
LIST="$OUT/../titles.json"

if [ ! -s "$LIST" ]; then
  echo "取 Category:武将 页面清单…"
  python3 - "$API" "$UA" "$LIST" <<'PY'
import json, sys, urllib.parse, urllib.request

api, ua, target = sys.argv[1], sys.argv[2], sys.argv[3]
params = {"action": "query", "list": "categorymembers",
          "cmtitle": "Category:武将", "cmlimit": "500", "format": "json"}
titles = []
while True:
    request = urllib.request.Request(api + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": ua})
    with urllib.request.urlopen(request, timeout=40) as response:
        data = json.loads(response.read().decode("utf-8"))
    titles += [item["title"] for item in data["query"]["categorymembers"]]
    if "continue" not in data:
        break
    params.update(data["continue"])
open(target, "w", encoding="utf-8").write(json.dumps(titles, ensure_ascii=False))
print(f"  {len(titles)} 个武将页")
PY
fi

python3 - "$API" "$OUT" "$UA" "$UA2" "$REFERER" <<'PY'
import json, sys, time, urllib.parse, urllib.request
from pathlib import Path

api, out, ua, ua2, referer = sys.argv[1:6]
out_dir = Path(out)
titles = json.loads((out_dir / ".." / "titles.json").read_text(encoding="utf-8"))
print(f"下载 {len(titles)} 个武将页 -> {out_dir}")

def fetch(title, user_agent, referer_header=None):
    params = {"action": "parse", "page": title, "prop": "wikitext|text",
              "format": "json", "redirects": "1"}
    headers = {"User-Agent": user_agent}
    if referer_header:
        headers["Referer"] = referer_header
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
    request = urllib.request.Request(api + "?" + urllib.parse.urlencode(params), headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))

failed = []
for index, title in enumerate(titles, 1):
    safe = urllib.parse.quote(title.replace("/", "_"), safe="")
    target = out_dir / f"{safe}.json"
    if target.exists() and target.stat().st_size > 200:
        continue
    data = None
    for attempt in range(3):
        try:
            data = fetch(title, ua)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  重试 {title}: {exc}", flush=True)
            time.sleep(3 + attempt * 3)
    if data is None or "error" in (data or {}):
        try:  # 换 UA + Referer 再试（站点 567 反爬）
            data = fetch(title, ua2, referer)
        except Exception as exc:  # noqa: BLE001
            print(f"  失败 {title}: {exc}", flush=True)
            failed.append(title)
            continue
    if "error" in data:
        print(f"  错误 {title}: {data['error'].get('info')}", flush=True)
        failed.append(title)
        continue
    target.write_text(json.dumps(data["parse"], ensure_ascii=False), encoding="utf-8")
    if index % 25 == 0:
        print(f"  {index}/{len(titles)}", flush=True)
    time.sleep(0.3)

if failed:
    print(f"还有 {len(failed)} 页没抓到，重跑本脚本会续抓：{'、'.join(failed[:10])}…")
PY

echo "完成。接下来：pip install pypinyin && python scripts/import_generals_wiki.py --pages-dir $OUT --apply"
