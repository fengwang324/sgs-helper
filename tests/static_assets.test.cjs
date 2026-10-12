"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const root = path.resolve(__dirname, "..");
const catalog = JSON.parse(fs.readFileSync(path.join(root, "web/catalog.json"), "utf8"));

test("browser data equals the review JSON and preserves all records", () => {
  const script = fs.readFileSync(path.join(root, "web/catalog.js"), "utf8").trim();
  const prefix = "window.SGS_CATALOG = ";
  assert.ok(script.startsWith(prefix) && script.endsWith(";"));
  assert.deepEqual(JSON.parse(script.slice(prefix.length, -1)), catalog);
  const collections = ["products", "generals", "general_variants", "skills", "skill_versions", "cards", "faq", "keywords"];
  assert.equal(collections.reduce((count, key) => count + catalog[key].length, 0), 1091);
});

test("every card image and initial page resource exists in the standalone site", () => {
  const images = new Set([...catalog.general_variants, ...catalog.cards].map(item => item.image).filter(Boolean));
  assert.equal(images.size, 277);
  for (const image of images) {
    assert.equal(path.basename(image), image);
    assert.ok(fs.existsSync(path.join(root, "web/assets/cards", image)), image);
  }
  assert.deepEqual(new Set(fs.readdirSync(path.join(root, "web/assets/cards"))), images);
  for (const faction of ["吴", "蜀", "魏", "群", "野", "晋"]) {
    assert.ok(fs.existsSync(path.join(root, "web/assets/factions", faction + ".png")));
  }
  const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
  for (const match of html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"/g)) {
    const resource = match[1].split("?")[0];
    assert.ok(!resource.includes(":") && !resource.startsWith("/"), resource);
    assert.ok(fs.existsSync(path.join(root, resource)), resource);
  }
  const app = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
  assert.ok(!app.includes("data/card_images/"));
  assert.ok(!/\bfetch\s*\(/.test(app));
});
