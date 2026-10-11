(function (root, factory) {
  "use strict";
  var exported = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  if (root) root.SGS_MODEL = exported;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  var collections = {general: "generals", skill: "skills", card: "cards", faq: "faq", keyword: "keywords", product: "products"};
  var alphabet = new Intl.Collator("zh-CN-u-co-pinyin", {numeric: true, sensitivity: "base"});
  var recastCardIds = new Set(["card_fen_dao_yang_biao", "card_lu_li_tong_xin", "card_tie_suo_lian_huan", "card_zhi_ji_zhi_bi"]);
  function cardSuitTokens(text) {
    text = String(text || ""); var out = [], symbols = "♥❤♡♦♢♠♤♣♧";
    for (var i = 0; i < text.length; i++) if (symbols.includes(text[i])) {
      var end = i + 1; if (text[end] === "\uFE0E" || text[end] === "\uFE0F") end++;
      out.push({start: i, end: end, suit: text[i]}); i = end - 1;
    }
    return out;
  }
  function isRecastCard(item) { return !!item && recastCardIds.has(item.id); }
  function keywordDefinitionRows(item, keywords) {
    if (!item) return [];
    var rows = (item.definition || "").split("\n").filter(Boolean);
    if (item.id === "keyword_104") return rows.filter(function (row) { return /^(主将技|副将技)：/.test(row); });
    if (item.id === "keyword_031") {
      var byName = new Map((keywords || []).map(function (keyword) { return [keyword.name, keyword]; }));
      ["队列", "围攻", "阵法召唤"].forEach(function (name) {
        var related = byName.get(name); if (related && related.definition) rows.push(name + "：" + related.definition);
      });
    }
    return rows;
  }
  var cardCategories = ["基本牌", "锦囊牌", "装备牌"];
  var cardTypes = {"基本牌": ["杀", "闪", "桃", "酒"], "锦囊牌": ["非延时锦囊", "延时锦囊"], "装备牌": ["武器", "防具", "坐骑", "宝物"]};
  function titleKey(item) {
    return String(item.name || item.question || "").replace(/^[^\p{L}\p{N}]+/u, "");
  }
  function compareEntries(a, b) {
    return alphabet.compare(titleKey(a), titleKey(b)) || alphabet.compare(a.id, b.id);
  }
  function rank(values, value) { var position = values.indexOf(value); return position < 0 ? values.length : position; }
  function factionParts(value) { return String(value || "").split(/[\/／&、]/).map(function (part) { return part.trim(); }).filter(Boolean); }
  function matchesFaction(value, selected) { return !selected || factionParts(value).includes(selected); }
  function numberTail(value) { var match = String(value || "").match(/(\d+)\s*$/); return match ? Number(match[1]) : Number.MAX_SAFE_INTEGER; }
  function compareGeneralVariants(a, b) {
    var aDouble = factionParts(a.faction).length > 1, bDouble = factionParts(b.faction).length > 1;
    return Number(aDouble) - Number(bDouble)
      || (aDouble && bDouble ? numberTail(a.general_number) - numberTail(b.general_number) : 0)
      || alphabet.compare(a.general_number || "", b.general_number || "")
      || alphabet.compare(a.id || "", b.id || "");
  }
  function compareCards(a, b) {
    return rank(cardCategories, a.category_2) - rank(cardCategories, b.category_2)
      || alphabet.compare(a.category_2 || "", b.category_2 || "")
      || rank(cardTypes[a.category_2] || [], a.category_3) - rank(cardTypes[b.category_2] || [], b.category_3)
      || alphabet.compare(a.category_3 || "", b.category_3 || "")
      || compareEntries(a, b);
  }
  function createModel(data) {
    var index = {};
    Object.keys(collections).forEach(function (kind) {
      index[kind] = new Map((data[collections[kind]] || []).map(function (item) { return [item.id, item]; }));
    });
    var variants = new Map(), versions = new Map(), versionById = new Map();
    (data.general_variants || []).forEach(function (v) {
      if (!variants.has(v.general_id)) variants.set(v.general_id, []);
      variants.get(v.general_id).push(v);
    });
    (data.skill_versions || []).forEach(function (v) {
      versionById.set(v.id, v);
      if (!versions.has(v.skill_id)) versions.set(v.skill_id, []);
      versions.get(v.skill_id).push(v);
    });
    function resolveVersion(skillId, context, explicit) {
      context = context || {};
      if (explicit) {
        var fixed = versionById.get(explicit);
        return fixed && fixed.skill_id === skillId ? fixed : null;
      }
      var rows = (versions.get(skillId) || []).slice();
      if (context.mode) {
        var exact = rows.filter(function (v) { return v.mode === context.mode; });
        rows = exact.length ? exact : rows.filter(function (v) { return v.mode === "common"; });
      }
      if (context.product_id) {
        var product = rows.filter(function (v) { return v.product_id === context.product_id; });
        var generic = rows.filter(function (v) { return !v.product_id; });
        if (product.length) rows = product;
        else if (generic.length) rows = generic;
        else return null;
      }
      return rows.length === 1 ? rows[0] : null;
    }
    var names = new Map();
    ["skill", "card", "keyword"].forEach(function (kind) {
      index[kind].forEach(function (item) {
        if (!item.name) return;
        if (!names.has(item.name)) names.set(item.name, []);
        names.get(item.name).push({kind: kind, id: item.id, name: item.name});
      });
    });
    var orderedNames = Array.from(names.keys()).sort(function (a, b) { return b.length - a.length || a.localeCompare(b, "zh-CN"); });
    function findMentions(text, context, exclude, kinds) {
      text = String(text || ""); exclude = new Set(exclude || []); context = context || {};
      var matches = [];
      for (var position = 0; position < text.length;) {
        var found = null;
        for (var n = 0; n < orderedNames.length; n++) {
          var name = orderedNames[n];
          if (!text.startsWith(name, position)) continue;
          var bracketPair = text[position - 1] + text[position + name.length];
          var quoted = ["【】", "“”", "「」", "『』", "《》", '""'].includes(bracketPair);
          if (name.length === 1) {
            var pair = text[position - 1] + text[position + 1];
            if (!["【】", "“”", "「」", "『』", "《》", '""'].includes(pair)) continue;
          }
          var targets = names.get(name).filter(function (t) {
            if (exclude.has(t.id) || (kinds && !kinds.includes(t.kind))) return false;
            // 连环 is also a rule state. A skill mention needs quotation or an explicit skill action.
            if (t.kind === "skill" && name === "连环" && !quoted) {
              return /(?:获得|拥有|发动|失去|技能)$/.test(text.slice(0, position)) || text.slice(position + name.length).startsWith("技能");
            }
            return true;
          });
          var skills = targets.filter(function (t) { return t.kind === "skill"; });
          if (skills.length > 1 && context.mode) {
            var applicable = skills.filter(function (t) { return resolveVersion(t.id, context); });
            if (applicable.length) targets = targets.filter(function (t) { return t.kind !== "skill"; }).concat(applicable);
          }
          if (!targets.length) continue;
          found = {start: position, end: position + name.length, label: name, targets: targets};
          break;
        }
        if (found) { matches.push(found); position = found.end; }
        else position++;
      }
      return matches;
    }
    function ownSkills(variant) {
      return (variant.skills || []).map(function (ref) {
        return {skill: index.skill.get(ref.skill_id), version: resolveVersion(ref.skill_id, variant, ref.version_id), ref: ref};
      }).filter(function (row) { return row.skill; });
    }
    function references(variant) {
      var own = ownSkills(variant), ownIds = new Set(own.map(function (r) { return r.skill.id; }));
      var visited = new Set(ownIds), referenced = new Map(), cards = new Map(), queue = own.slice();
      while (queue.length) {
        var row = queue.shift();
        if (!row.version) continue;
        findMentions(row.version.text, variant, [row.skill.id], ["skill", "card"]).forEach(function (m) {
          m.targets.forEach(function (target) {
            if (target.kind === "card") cards.set(target.id, index.card.get(target.id));
            if (target.kind !== "skill" || visited.has(target.id)) return;
            visited.add(target.id);
            var next = {skill: index.skill.get(target.id), version: resolveVersion(target.id, variant), from: row.skill.name};
            referenced.set(target.id, next);
            if (next.version) queue.push(next);
          });
        });
      }
      return {skills: Array.from(referenced.values()), cards: Array.from(cards.values())};
    }
    function matchesQuery(text, query) {
      var haystack = String(text || "").toLocaleLowerCase();
      return String(query || "").trim().toLocaleLowerCase().split(/\s+/).filter(Boolean).every(function (term) { return haystack.includes(term); });
    }
    function searchable(kind, item) {
      if (kind === "general") {
        return [item.name].concat((variants.get(item.id) || []).map(function (v) {
          return [v.title, v.general_number, v.general_set, v.faction, v.zhulianbihe.join(" "), ownSkills(v).map(function (r) { return r.skill.name + " " + (r.version ? r.version.text : ""); }).join(" ")].join(" ");
        })).join(" ");
      }
      if (kind === "skill") return item.name + " " + (versions.get(item.id) || []).map(function (v) { return v.text; }).join(" ");
      if (kind === "card") return [item.name, item.category_1, item.category_2, item.category_3, item.card_set, item.effect, item.attack_range, item.deck_info, item.note, JSON.stringify(item.rules || {})].join(" ");
      if (kind === "product") return [item.name, item.release_year, item.mode, (item.general_sets || []).join(" "), (item.card_sets || []).join(" ")].join(" ");
      if (kind === "faq") return item.question + " " + item.answer;
      return item.name + " " + (item.definition || "");
    }
    function relatedFaq(namesToFind) {
      return (data.faq || []).filter(function (faq) {
        var text = faq.question + " " + faq.answer;
        return namesToFind.some(function (name) { return name.length > 1 && text.includes(name); });
      }).sort(compareEntries);
    }
    function compareGenerals(a, b) {
      var av = (variants.get(a.id) || []).slice().sort(compareGeneralVariants)[0] || {};
      var bv = (variants.get(b.id) || []).slice().sort(compareGeneralVariants)[0] || {};
      return compareGeneralVariants(av, bv) || alphabet.compare(a.name || "", b.name || "") || alphabet.compare(a.id, b.id);
    }
    return {data: data, index: index, variants: variants, versions: versions, versionById: versionById, resolveVersion: resolveVersion, findMentions: findMentions, ownSkills: ownSkills, references: references, matchesQuery: matchesQuery, searchable: searchable, relatedFaq: relatedFaq, compareEntries: compareEntries, compareCards: compareCards, compareGenerals: compareGenerals, compareGeneralVariants: compareGeneralVariants, factionParts: factionParts, matchesFaction: matchesFaction, cardSuitTokens: cardSuitTokens, isRecastCard: isRecastCard, keywordDefinitionRows: keywordDefinitionRows, cardCategories: cardCategories, cardTypes: cardTypes};
  }
  return {createModel: createModel};
});
