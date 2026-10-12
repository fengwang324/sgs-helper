"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { createModel } = require("../web/model.js");
const catalog = require("../web/catalog.json");

const model = createModel(catalog);

test("faction filters match either half of a dual-faction general", () => {
  assert.deepEqual(model.factionParts("群/魏"), ["群", "魏"]);
  assert.equal(model.matchesFaction("群/魏", "群"), true);
  assert.equal(model.matchesFaction("群/魏", "魏"), true);
  assert.equal(model.matchesFaction("群/魏", "蜀"), false);
  assert.equal(model.matchesFaction("魏", ""), true);
});

test("single-faction generals sort by natural number and dual-faction generals follow", () => {
  const rows = [
    { id: "double", name: "双势力", general_number: "G.WU&SHU 002", faction: "吴/蜀" },
    { id: "ten", name: "十号", general_number: "G.WU 010", faction: "吴" },
    { id: "two", name: "二号", general_number: "G.WU 002", faction: "吴" },
  ];
  assert.deepEqual(rows.sort(model.compareGeneralVariants).map((row) => row.id), ["two", "ten", "double"]);
});

test("skill and keyword entries use the same pinyin ordering", () => {
  const rows = [{ id: "locked", name: "锁定技" }, { id: "array", name: "阵法技" }, { id: "limit", name: "限定技" }];
  assert.deepEqual(rows.sort(model.compareEntries).map((row) => row.name), ["锁定技", "限定技", "阵法技"]);
});

test("card suit tokenization keeps Unicode variants and consumes optional variation selectors", () => {
  assert.deepEqual(model.cardSuitTokens("♥❤\uFE0F♡♦♢♠♤♣♧").map((token) => token.suit), ["♥", "❤", "♡", "♦", "♢", "♠", "♤", "♣", "♧"]);
  assert.equal(model.cardSuitTokens("♥❤\uFE0F♡♦♢♠♤♣♧")[1].end, 3);
});

test("only the four source-confirmed cards receive the recast label", () => {
  const recast = ["card_fen_dao_yang_biao", "card_lu_li_tong_xin", "card_tie_suo_lian_huan", "card_zhi_ji_zhi_bi"];
  assert.deepEqual(catalog.cards.filter(model.isRecastCard).map((card) => card.id).sort(), recast.slice().sort());
  assert.equal(model.isRecastCard({ id: "card_wu_zhong_sheng_you" }), false);
});


test("glossary rows split main and deputy general skills and extend formation rules from keyword data", () => {
  const lead = catalog.keywords.find((item) => item.id === "keyword_104");
  assert.deepEqual(model.keywordDefinitionRows(lead, catalog.keywords), lead.definition.split("\n"));
  const formation = catalog.keywords.find((item) => item.id === "keyword_031");
  const rows = model.keywordDefinitionRows(formation, catalog.keywords);
  assert.equal(rows.length, 5);
  for (const name of ["队列", "围攻", "阵法召唤"]) {
    const keyword = catalog.keywords.find((item) => item.name === name);
    assert.ok(rows.includes(name + "：" + keyword.definition));
  }
});

test("all five ruler opening skills link to their existing indicator and deduplicate repeated mentions", () => {
  const rulers = [
    ["general_g_em_001", "五虎将大旗", "card_n40e425c9"],
    ["general_g_em_002", "黄巾天兵符", "card_n271a9ca0"],
    ["general_g_em_003", "缘江烽火图", "card_ndb7ea177"],
    ["general_g_em_004", "五子良将纛", "card_naf71007c"],
    ["general_g_em_005", "八荒死士令", "card_n91452981"],
  ];
  for (const [generalId, name, id] of rulers) {
    const variant = model.variants.get(generalId)[0];
    const first = model.ownSkills(variant)[0];
    const mentions = model.findMentions(first.version.text, variant, [first.skill.id], ["card"]);
    assert.ok(mentions.length > 0, name);
    for (const mention of mentions) {
      assert.equal(mention.label, name);
      assert.deepEqual(mention.targets.map((target) => target.id), [id]);
    }
    const references = model.references(variant).cards;
    assert.deepEqual(references.filter((card) => card.category_1 === "指示物").map((card) => card.id), [id]);
    const item = model.index.card.get(id);
    assert.equal(item.name, name);
    assert.equal(model.matchesQuery(model.searchable("card", item), name), true);
    assert.equal(model.matchesQuery(model.searchable("card", item), item.name), true);
    // Any equipment already named in the skill still resolves to the same record.
    for (const skill of model.ownSkills(variant)) {
      for (const card of catalog.cards.filter((card) => card.category_2 === "装备牌")) {
        if (skill.version.text.includes("【" + card.name + "】")) {
          assert.ok(references.some((reference) => reference.id === card.id), card.name);
        }
      }
    }
  }
});

test("ruler indicators are not assigned implicitly and missing records are not invented", () => {
  const ordinaryLiuBei = model.variants.get("general_g_shu_01")[0];
  assert.equal(model.references(ordinaryLiuBei).cards.some((card) => card.id === "card_n40e425c9"), false);
  const empty = createModel({});
  assert.deepEqual(empty.findMentions("五虎将大旗、缘江烽火图、八荒死士令", {}, [], ["card"]), []);
});
