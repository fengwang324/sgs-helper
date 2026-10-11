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

