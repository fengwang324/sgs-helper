#!/usr/bin/env bash
# 抓取 gltjk.com《三国杀官方规则集 3.0》的页面到本地，供 scripts/import_rules_book.py 解析。
#
#   bash scripts/import_rules_book.sh [输出目录]      # 默认 /tmp/rules
#
# 只做“下载”这一件事：导入逻辑在 scripts/import_rules_book.py。
set -euo pipefail

BASE="https://gltjk.com/sanguosha/rules"
OUT="${1:-/tmp/rules}"
PAGES=(
  card/hero
  card/hero/wei
  card/hero/shu
  card/hero/wu
  card/hero/neutral
  card/hero/legend
  card/hero/west
  card/hero/nongame
  glossary/card
  glossary/cardinfo
  glossary/zone
  glossary/value
  glossary/description
  glossary/flow
  glossary/event
  glossary/gamecard
  glossary/herocard
  glossary/guo
  flow/game
  flow/use
  flow/play
  flow/rankcompare
  flow/move
  flow/statechange
  flow/judge
  flow/damage
  flow/loselife
  flow/decreaselife
  flow/recoverlife
  flow/decreaselifemax
  flow/increaselifemax
  flow/neardeath
  flow/death
  flow/condition
  flow/virtual
  rule/principle
  rule/element
  rule/forced
  rule/classification
  appendix/mizhimie
)

mkdir -p "$OUT"
for page in "${PAGES[@]}"; do
  file="$OUT/$(echo "$page" | tr '/' '_').html"
  if [ -s "$file" ]; then
    echo "cached  $page"
    continue
  fi
  code=$(curl -sS -o "$file" -w "%{http_code}" --max-time 30 "$BASE/$page.html")
  echo "$code     $page"
  if [ "$code" != "200" ]; then
    echo "抓取失败：$page（HTTP $code）" >&2
    exit 1
  fi
done

echo "已下载到 $OUT，接下来：python scripts/import_rules_book.py --pages-dir $OUT --stage all"
