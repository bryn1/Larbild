/* Tests for the Bibliotek pure data layer (lb-eee).
 *
 * Runs under Node's built-in test runner (zero dependencies, no build step):
 *     node --test tests/bibliotek_data.test.mjs
 *
 * Pins the ordering + lookup logic the React view relies on — most importantly
 * the bead's "fysik/teknik/kemi/biologi are presented first" requirement and
 * the count/central-content reads over the /api/library (lb-5rc) response shape.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const D = require("../static/js/bibliotek_data.js");

// A small fixture shaped like the GET /api/library response (lb-5rc contract).
function fixture() {
  const area = (id, name, count = 0, central = []) => ({
    id, name, glyph: "▢", asset_count: count,
    central_content: central.map((t, i) => ({ id: `${id}:c${i + 1}`, text: t, asset_count: 0 })),
  });
  const subj = (id, name, hue, count, areas) => ({
    id, name, short: name.slice(0, 3), hue, glyph: "◇", blurb: "…",
    asset_count: count, areas,
  });
  // Intentionally NOT in priority order, to prove orderSubjects reorders.
  return [
    subj("matematik", "Matematik", 250, 0, [area("taluppfattning", "Taluppfattning", 0, ["Bråk", "decimaltal", "procent"])]),
    subj("biologi", "Biologi", 150, 3, [area("celler", "Celler", 3, ["Cellens delar"])]),
    subj("fysik", "Fysik", 248, 5, [area("ljud", "Ljud", 2, ["Ljudvågor"]), area("ljus", "Ljus", 3, ["Reflektion", "brytning"])]),
    subj("kemi", "Kemi", 200, 0, [area("vatten", "Vatten", 0, ["pH", "syror"])]),
    subj("teknik", "Teknik", 30, 1, [area("system", "System", 1, ["Styr- och reglerteknik"])]),
    subj("historia", "Historia", 40, 0, [area("medeltiden", "Medeltiden", 0, ["Vikingatiden"])]),
  ];
}

test("orderSubjects puts fysik/teknik/kemi/biologi first, in that order", () => {
  const ordered = D.orderSubjects(fixture());
  assert.deepEqual(ordered.slice(0, 4).map((s) => s.id), ["fysik", "teknik", "kemi", "biologi"]);
});

test("orderSubjects keeps non-priority subjects after, in original order", () => {
  const ordered = D.orderSubjects(fixture());
  const tail = ordered.slice(4).map((s) => s.id);
  // matematik appeared before historia in the input → stable tail order.
  assert.deepEqual(tail, ["matematik", "historia"]);
});

test("orderSubjects does not mutate its input", () => {
  const subs = fixture();
  const before = subs.map((s) => s.id);
  D.orderSubjects(subs);
  assert.deepEqual(subs.map((s) => s.id), before);
});

test("orderSubjects tolerates empty / missing input", () => {
  assert.deepEqual(D.orderSubjects([]), []);
  assert.deepEqual(D.orderSubjects(undefined), []);
});

test("findSubject / findArea locate nodes by id", () => {
  const subs = fixture();
  const fysik = D.findSubject(subs, "fysik");
  assert.equal(fysik.name, "Fysik");
  assert.equal(D.findSubject(subs, "nope"), null);
  assert.equal(D.findArea(fysik, "ljud").name, "Ljud");
  assert.equal(D.findArea(fysik, "nope"), null);
  assert.equal(D.findArea(null, "ljud"), null);
});

test("totalAssets sums subject asset_count (not re-summing areas)", () => {
  // 0 + 3 + 5 + 0 + 1 + 0 = 9
  assert.equal(D.totalAssets(fixture()), 9);
  assert.equal(D.totalAssets([]), 0);
});

test("centralText rejoins central_content entries into the panel sentence", () => {
  const subs = fixture();
  const fysik = D.findSubject(subs, "fysik");
  const ljus = D.findArea(fysik, "ljus");
  assert.equal(D.centralText(ljus), "Reflektion, brytning");
  assert.equal(D.centralText(null), "");
  assert.equal(D.centralText({ id: "x" }), ""); // no central_content
});

test("subjColor / subjSoft build OKLCH from the subject hue (default 268)", () => {
  const fysik = D.findSubject(fixture(), "fysik");
  assert.equal(D.subjColor(fysik), "oklch(0.55 0.15 248)");
  assert.equal(D.subjSoft(fysik), "oklch(0.95 0.05 248)");
  // default when no subject / no hue
  assert.equal(D.subjColor(null), "oklch(0.55 0.15 268)");
  assert.equal(D.subjSoft(undefined), "oklch(0.95 0.05 268)");
});

test("PRIORITY_SUBJECTS is exactly the four science subjects", () => {
  assert.deepEqual(D.PRIORITY_SUBJECTS, ["fysik", "teknik", "kemi", "biologi"]);
});

/* ---------- the Skapa deep-link the "Skapa här" buttons use (lb-skapafix) ---------- */

test("skapaLink carries the browse context into the funnel route", () => {
  // top-level "Skapa bild": no context → bare funnel route (funnel defaults)
  assert.equal(D.skapaLink(), "/skapa");
  assert.equal(D.skapaLink(null), "/skapa");
  assert.equal(D.skapaLink(undefined, "ljud"), "/skapa"); // area without subject is dropped
  // a subject alone (browsing a subject, no area picked)
  assert.equal(D.skapaLink("fysik"), "/skapa?subject=fysik");
  assert.equal(D.skapaLink("fysik", null), "/skapa?subject=fysik");
  // subject + area (the "Skapa här" case from inside an area)
  assert.equal(D.skapaLink("fysik", "ljud"), "/skapa?subject=fysik&area=ljud");
});

test("skapaLink url-encodes both ids (defensive — never inject)", () => {
  assert.equal(D.skapaLink("a b", "c&d"), "/skapa?subject=a%20b&area=c%26d");
});
