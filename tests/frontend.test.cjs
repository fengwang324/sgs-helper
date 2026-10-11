"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { createModel } = require("../web/model.js");
const catalog = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "web", "catalog.json"), "utf8")
);

test("catalog keeps same-name generals separate and resolves every variant skill binding", () => {
  const model = createModel(catalog);
  assert.equal(catalog.general_variants.length, 163);

  const sameName = catalog.generals.filter((general) => general.name === "司马昭");
  assert.equal(sameName.length, 2);
  assert.notEqual(sameName[0].id, sameName[1].id);
  const sameNameNumbers = sameName.map((general) => {
    const variants = model.variants.get(general.id);
    assert.equal(variants.length, 1);
    return variants[0].general_number;
  });
  assert.deepEqual(new Set(sameNameNumbers), new Set(["G.AM 001", "G.JIN 004"]));

  const skills = new Map(catalog.skills.map((skill) => [skill.id, skill]));
  const versions = new Map(catalog.skill_versions.map((version) => [version.id, version]));
  let referenceCount = 0;
  for (const variant of catalog.general_variants) {
    assert.ok(model.index.general.has(variant.general_id), variant.id);
    for (const ref of variant.skills) {
      referenceCount += 1;
      assert.ok(skills.has(ref.skill_id), `${variant.id} -> ${ref.skill_id}`);
      assert.ok(ref.version_id, `${variant.id} has an explicit version binding`);
      const version = versions.get(ref.version_id);
      assert.ok(version, `${variant.id} -> missing ${ref.version_id}`);
      assert.equal(version.skill_id, ref.skill_id, `${variant.id} -> ${ref.version_id}`);
      assert.equal(model.resolveVersion(ref.skill_id, variant, ref.version_id), version);
    }
  }
  assert.equal(referenceCount, 293);
});

test("version lookup prefers mode and product matches and returns null for conflicts", () => {
  const model = createModel({
    skills: [{ id: "skill_pick", name: "择版" }, { id: "skill_conflict", name: "冲突" }],
    skill_versions: [
      { id: "pick-common", skill_id: "skill_pick", mode: "common", product_id: null, text: "通用" },
      { id: "pick-guozhan", skill_id: "skill_pick", mode: "guozhan", product_id: null, text: "国战通用" },
      { id: "pick-guozhan-a", skill_id: "skill_pick", mode: "guozhan", product_id: "product_a", text: "国战产品甲" },
      { id: "pick-identity", skill_id: "skill_pick", mode: "identity", product_id: null, text: "身份局" },
      { id: "pick-mobile", skill_id: "skill_pick", mode: "common", product_id: "product_a", text: "通用产品甲" },
      { id: "conflict-a", skill_id: "skill_conflict", mode: "identity", product_id: "product_a", text: "候选甲" },
      { id: "conflict-b", skill_id: "skill_conflict", mode: "identity", product_id: "product_a", text: "候选乙" },
    ],
  });

  assert.equal(model.resolveVersion("skill_pick", { mode: "guozhan", product_id: "product_a" }).id, "pick-guozhan-a");
  assert.equal(model.resolveVersion("skill_pick", { mode: "guozhan", product_id: "product_b" }).id, "pick-guozhan");
  assert.equal(model.resolveVersion("skill_pick", { mode: "identity", product_id: "product_a" }).id, "pick-identity");
  assert.equal(model.resolveVersion("skill_pick", { mode: "mobile", product_id: "product_a" }).id, "pick-mobile");
  assert.equal(model.resolveVersion("skill_conflict", { mode: "identity", product_id: "product_a" }), null);
  assert.equal(model.resolveVersion("skill_conflict", { mode: "identity" }), null);
  assert.equal(model.resolveVersion("skill_pick", {}, "conflict-a"), null);
});

test("recursive skill references deduplicate cycles and repeated cards", () => {
  const data = {
    skills: [
      { id: "skill_a", name: "技能甲" },
      { id: "skill_b", name: "技能乙" },
      { id: "skill_c", name: "技能丙" },
    ],
    skill_versions: [
      { id: "version_a", skill_id: "skill_a", mode: "guozhan", product_id: null, text: "技能乙" },
      { id: "version_b", skill_id: "skill_b", mode: "guozhan", product_id: null, text: "技能丙、技能甲" },
      { id: "version_c", skill_id: "skill_c", mode: "guozhan", product_id: null, text: "技能乙、技能乙、【闪】、【闪】" },
    ],
    cards: [{ id: "card_flash", name: "闪", category_1: "游戏牌" }],
    general_variants: [{
      id: "variant_a",
      mode: "guozhan",
      skills: [{ skill_id: "skill_a", version_id: "version_a" }],
    }],
  };
  const model = createModel(data);
  const result = model.references(data.general_variants[0]);

  assert.deepEqual(result.skills.map((row) => row.skill.id).sort(), ["skill_b", "skill_c"]);
  assert.deepEqual(result.cards.map((card) => card.id), ["card_flash"]);
});

test("mentions choose the longest name and require brackets around a one-character name", () => {
  const model = createModel({
    skills: [
      { id: "skill_sha", name: "杀" },
      { id: "skill_fire_sha", name: "火杀" },
    ],
  });

  const longest = model.findMentions("火杀");
  assert.equal(longest.length, 1);
  assert.equal(longest[0].label, "火杀");
  assert.deepEqual(longest[0].targets.map((target) => target.id), ["skill_fire_sha"]);
  assert.deepEqual(model.findMentions("杀死"), []);
  assert.deepEqual(model.findMentions("【杀】").map((match) => match.label), ["杀"]);
});

test("keyword phrases do not hide skill names when only skill and card links are requested", () => {
  const model = createModel({
    skills: [{ id: "skill_ying_zi", name: "英姿" }],
    keywords: [{ id: "keyword_phrase", name: "获得英姿" }],
  });
  assert.deepEqual(model.findMentions("获得英姿", {}, [], ["skill", "card"]).map((m) => m.targets[0].id), ["skill_ying_zi"]);
});

test("rule descriptions about the chained state do not grant the 连环 skill", () => {
  const model = createModel({skills:[{id:"skill_lian_huan",name:"连环"}]});
  assert.deepEqual(model.findMentions("处于连环状态，火杀连环传导，通用连环渠道"), []);
  assert.equal(model.findMentions("获得“连环”")[0].targets[0].id, "skill_lian_huan");
  assert.equal(model.findMentions("发动连环")[0].targets[0].id, "skill_lian_huan");
});

test("alphabetical indexes use Chinese pinyin and ignore opening punctuation", () => {
  const model = createModel({});
  const rows = [{id:"j",question:"【酒】的效果是什么？"},{id:"h",question:"火杀如何结算？"},{id:"a",question:"暗将如何明置？"}];
  assert.deepEqual(rows.sort(model.compareEntries).map((r) => r.id), ["a","h","j"]);
});

test("game cards group by category and type before alphabetical names", () => {
  const model = createModel({});
  const rows = [
    {id:"w",name:"八卦阵",category_2:"装备牌",category_3:"防具"},
    {id:"d",name:"兵粮寸断",category_2:"锦囊牌",category_3:"延时锦囊"},
    {id:"n",name:"无中生有",category_2:"锦囊牌",category_3:"非延时锦囊"},
    {id:"s",name:"闪",category_2:"基本牌",category_3:"闪"},
    {id:"l",name:"雷杀",category_2:"基本牌",category_3:"杀"},
    {id:"h",name:"火杀",category_2:"基本牌",category_3:"杀"},
  ];
  assert.deepEqual(rows.sort(model.compareCards).map((r) => r.id), ["h","l","s","n","d","w"]);
});
