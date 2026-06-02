/* Tests for the Skapa funnel pure data layer (lb-e1p / P3.1).
 *
 * Runs under Node's built-in test runner (zero dependencies, no build step):
 *     node --test tests/skapa_data.test.mjs
 *
 * Pins the funnel logic the React view relies on: science-first subject order,
 * node lookup, submit-readiness (the client mirror of the /api/jobs required
 * fields), the destination label, and — most load-bearing — the EXACT job
 * payload builder, which must NEVER include a `lora` field (the worker resolves
 * + allowlist-validates it server-side; the SFW guard). No live calls.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const S = require("../static/js/skapa_data.js");

// A small fixture shaped like GET /api/taxonomy (as_dict(), lb-582).
function tax() {
  const area = (slug, name, central) => ({ slug, name, glyph: "▢", central });
  const subj = (slug, name, hue, areas) => ({
    slug, name, short: name.slice(0, 2).toUpperCase(), hue, glyph: "◇", blurb: "…", areas,
  });
  return {
    // Intentionally NOT in priority order, to prove orderSubjects reorders.
    subjects: [
      subj("matematik", "Matematik", 18, [area("taluppfattning", "Taluppfattning", "Bråk, decimaltal.")]),
      subj("biologi", "Biologi", 135, [area("celler", "Celler", "Cellens delar.")]),
      subj("fysik", "Fysik", 248, [area("ljud", "Ljud", "Ljudvågor."), area("ljus", "Ljus", "Reflektion, brytning.")]),
      subj("kemi", "Kemi", 150, [area("vatten", "Vatten", "pH, syror.")]),
      subj("teknik", "Teknik", 60, [area("system", "System", "Styr- och reglerteknik.")]),
      subj("historia", "Historia", 50, [area("medeltiden", "Medeltiden", "Vikingatiden.")]),
    ],
    imageTypes: [
      { slug: "foto", name: "Fotorealistisk", glyph: "◉", desc: "…" },
      { slug: "illustration", name: "Vetenskaplig illustration", glyph: "✎", desc: "…" },
      { slug: "diagram", name: "Schematiskt diagram", glyph: "⊟", desc: "…" },
    ],
  };
}

/* ---------- ordering + lookup ---------- */

test("orderSubjects puts fysik/teknik/kemi/biologi first, in that order", () => {
  const ordered = S.orderSubjects(tax().subjects);
  assert.deepEqual(ordered.slice(0, 4).map((s) => s.slug), ["fysik", "teknik", "kemi", "biologi"]);
});

test("orderSubjects keeps non-priority subjects after, in original order", () => {
  const ordered = S.orderSubjects(tax().subjects);
  assert.deepEqual(ordered.slice(4).map((s) => s.slug), ["matematik", "historia"]);
});

test("orderSubjects does not mutate its input + tolerates empty", () => {
  const subs = tax().subjects;
  const before = subs.map((s) => s.slug);
  S.orderSubjects(subs);
  assert.deepEqual(subs.map((s) => s.slug), before);
  assert.deepEqual(S.orderSubjects([]), []);
  assert.deepEqual(S.orderSubjects(undefined), []);
});

test("findSubject / findArea / findImageType locate nodes by slug", () => {
  const t = tax();
  const fysik = S.findSubject(t.subjects, "fysik");
  assert.equal(fysik.name, "Fysik");
  assert.equal(S.findSubject(t.subjects, "nope"), null);
  assert.equal(S.findArea(fysik, "ljud").name, "Ljud");
  assert.equal(S.findArea(fysik, "nope"), null);
  assert.equal(S.findArea(null, "ljud"), null);
  assert.equal(S.findImageType(t.imageTypes, "diagram").name, "Schematiskt diagram");
  assert.equal(S.findImageType(t.imageTypes, "nope"), null);
});

/* ---------- hue / colour ---------- */

test("subjectHue + subjColor derive from hue with the 268 default", () => {
  const fysik = S.findSubject(tax().subjects, "fysik");
  assert.equal(S.subjectHue(fysik), 248);
  assert.equal(S.subjectHue(null), 268);
  assert.equal(S.subjColor(fysik), "oklch(0.55 0.15 248)");
  assert.equal(S.subjColor(null), "oklch(0.55 0.15 268)");
});

/* ---------- destination label ---------- */

test("destinationLabel reflects subject › area, prompting when incomplete", () => {
  const t = tax();
  const fysik = S.findSubject(t.subjects, "fysik");
  const ljud = S.findArea(fysik, "ljud");
  assert.equal(S.destinationLabel(fysik, ljud), "Fysik › Ljud");
  assert.equal(S.destinationLabel(fysik, null), "Fysik › (välj område)");
  assert.equal(S.destinationLabel(null, null), "(välj ämne) › (välj område)");
});

/* ---------- submit readiness (client mirror of /api/jobs required fields) ---------- */

test("submitReadiness requires subject, area, imageType, and a non-empty prompt", () => {
  const t = tax();
  const subject = S.findSubject(t.subjects, "fysik");
  const area = S.findArea(subject, "ljud");
  const imageType = S.findImageType(t.imageTypes, "foto");

  assert.deepEqual(S.submitReadiness({}), { ok: false, reason: "välj ämne" });
  assert.deepEqual(S.submitReadiness({ subject }), { ok: false, reason: "välj område" });
  assert.deepEqual(S.submitReadiness({ subject, area }), { ok: false, reason: "välj bildtyp" });
  assert.deepEqual(S.submitReadiness({ subject, area, imageType, prompt: "  " }),
    { ok: false, reason: "skriv en beskrivning" });
  assert.deepEqual(S.submitReadiness({ subject, area, imageType, prompt: "en krets" }),
    { ok: true, reason: "" });
});

/* ---------- seed / count normalisation ---------- */

test("normalizeSeed: blank → null, valid int kept, negative/NaN → null", () => {
  assert.equal(S.normalizeSeed(""), null);
  assert.equal(S.normalizeSeed(null), null);
  assert.equal(S.normalizeSeed(undefined), null);
  assert.equal(S.normalizeSeed("12345"), 12345);
  assert.equal(S.normalizeSeed(42), 42);
  assert.equal(S.normalizeSeed(3.9), 3); // truncated
  assert.equal(S.normalizeSeed(-1), null);
  assert.equal(S.normalizeSeed("abc"), null);
});

test("clampCount clamps to 1..8 or null when absent/invalid", () => {
  assert.equal(S.clampCount(""), null);
  assert.equal(S.clampCount(undefined), null);
  assert.equal(S.clampCount(1), 1);
  assert.equal(S.clampCount(8), 8);
  assert.equal(S.clampCount(0), 1);
  assert.equal(S.clampCount(99), 8);
  assert.equal(S.clampCount("nope"), null);
});

/* ---------- the job payload builder (the SFW-critical contract) ---------- */

test("buildJobPayload produces the exact /api/jobs body with slugs + trimmed prompt", () => {
  const body = S.buildJobPayload({
    subject: "fysik", area: "ljud", imageType: "diagram", prompt: "  ett kraftdiagram  ",
  });
  assert.deepEqual(body, {
    kind: "bild",
    subject: "fysik",
    area: "ljud",
    imageType: "diagram",
    prompt: "ett kraftdiagram",
  });
});

test("buildJobPayload NEVER includes a lora — even if one is passed in params", () => {
  const body = S.buildJobPayload({
    subject: "fysik", area: "ljud", imageType: "diagram", prompt: "x",
    params: { lora: "evil-nsfw-lora", loraName: "also-evil", foo: "bar" },
  });
  assert.equal("lora" in body, false);
  assert.equal("loraName" in body, false);
  // unrelated provenance params survive, minus the lora keys
  assert.deepEqual(body.params, { foo: "bar" });
});

test("buildJobPayload includes seed only when valid; count rides in params", () => {
  const withSeed = S.buildJobPayload({
    subject: "kemi", area: "vatten", imageType: "foto", prompt: "x", seed: "777", count: 3,
  });
  assert.equal(withSeed.seed, 777);
  assert.equal(withSeed.params.count, 3);

  const noSeed = S.buildJobPayload({
    subject: "kemi", area: "vatten", imageType: "foto", prompt: "x", seed: "", count: 1,
  });
  assert.equal("seed" in noSeed, false);
  // count of 1 is the default, still recorded for provenance
  assert.equal(noSeed.params.count, 1);

  const badSeed = S.buildJobPayload({
    subject: "kemi", area: "vatten", imageType: "foto", prompt: "x", seed: "-5",
  });
  assert.equal("seed" in badSeed, false);
});

test("buildJobPayload adds an optional title when non-blank", () => {
  const body = S.buildJobPayload({
    subject: "fysik", area: "ljud", imageType: "diagram", prompt: "x", title: "  Ljudvågor  ",
  });
  assert.equal(body.title, "Ljudvågor");
  const noTitle = S.buildJobPayload({
    subject: "fysik", area: "ljud", imageType: "diagram", prompt: "x", title: "   ",
  });
  assert.equal("title" in noTitle, false);
});
