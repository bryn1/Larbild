/* Tests for the Bibliotek review + regenerate data layer (lb-review-regen).
 *
 * Runs under Node's built-in test runner (zero deps, no build step):
 *     node --test tests/bibliotek_review_regen.test.mjs
 *
 * Pins the pure logic the React review/regenerate UI relies on:
 *   • review verdict labels/classes + the gallery filter predicate,
 *   • version-chain grouping over parent_id (the regenerate history),
 *   • the URL <-> browse-location round-trip that fixes the refresh/nav bug.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const D = require("../static/js/bibliotek_data.js");

/* ── review verdict helpers ─────────────────────────────────────────────── */

test("REVIEW tri-state constants", () => {
  assert.equal(D.REVIEW.PENDING, null);
  assert.equal(D.REVIEW.OK, 1);
  assert.equal(D.REVIEW.NOTOK, 0);
});

test("reviewLabel maps the tri-state to Swedish", () => {
  assert.equal(D.reviewLabel(1), "OK");
  assert.equal(D.reviewLabel(0), "Inte OK");
  assert.equal(D.reviewLabel(null), "Ej granskad");
  assert.equal(D.reviewLabel(undefined), "Ej granskad");
});

test("reviewClass maps the tri-state to a CSS state", () => {
  assert.equal(D.reviewClass(1), "ok");
  assert.equal(D.reviewClass(0), "notok");
  assert.equal(D.reviewClass(null), "pending");
});

test("toggleVerdict clears the active verdict, switches otherwise (the review control)", () => {
  // from pending: clicking OK → OK, clicking Inte-OK → Inte-OK
  assert.equal(D.toggleVerdict(null, 1), 1);
  assert.equal(D.toggleVerdict(null, 0), 0);
  // clicking the already-set verdict clears back to pending
  assert.equal(D.toggleVerdict(1, 1), null);
  assert.equal(D.toggleVerdict(0, 0), null);
  // clicking the other verdict switches
  assert.equal(D.toggleVerdict(1, 0), 0);
  assert.equal(D.toggleVerdict(0, 1), 1);
});

test("matchesReview filters by verdict", () => {
  const ok = { review_ok: 1 }, notok = { review_ok: 0 }, pending = { review_ok: null }, missing = {};
  // 'all'/absent → everything
  for (const it of [ok, notok, pending, missing]) {
    assert.equal(D.matchesReview(it, "all"), true);
    assert.equal(D.matchesReview(it, ""), true);
  }
  assert.equal(D.matchesReview(ok, "ok"), true);
  assert.equal(D.matchesReview(notok, "ok"), false);
  assert.equal(D.matchesReview(notok, "notok"), true);
  assert.equal(D.matchesReview(ok, "notok"), false);
  // pending matches both an explicit null and a missing field
  assert.equal(D.matchesReview(pending, "pending"), true);
  assert.equal(D.matchesReview(missing, "pending"), true);
  assert.equal(D.matchesReview(ok, "pending"), false);
});

/* ── version chains ─────────────────────────────────────────────────────── */

const it = (id, parent_id, created_at) => ({ id, parent_id, created_at, kind: "bild" });

test("buildVersionChains groups a flat list into chains, root→latest ordered", () => {
  // v1 (root) → v2 → v3, plus a standalone single-version asset.
  const items = [
    it("v3", "v2", "2026-01-03"),
    it("v1", null, "2026-01-01"),
    it("v2", "v1", "2026-01-02"),
    it("solo", null, "2026-01-04"),
  ];
  const chains = D.buildVersionChains(items);
  assert.equal(chains.length, 2);
  const byRoot = Object.fromEntries(chains.map((c) => [c.root.id, c]));
  // the v1 chain has all three in created order, latest = v3
  assert.deepEqual(byRoot.v1.versions.map((v) => v.id), ["v1", "v2", "v3"]);
  assert.equal(byRoot.v1.latest.id, "v3");
  // the solo asset is its own chain
  assert.deepEqual(byRoot.solo.versions.map((v) => v.id), ["solo"]);
  assert.equal(byRoot.solo.latest.id, "solo");
});

test("buildVersionChains treats an item whose parent is absent as its own root", () => {
  // The page is area-scoped/filtered, so a parent can legitimately be missing.
  const items = [it("child", "missing-parent", "2026-01-02")];
  const chains = D.buildVersionChains(items);
  assert.equal(chains.length, 1);
  assert.equal(chains[0].root.id, "child");
  assert.equal(chains[0].latest.id, "child");
});

test("buildVersionChains keeps roots ordered by their latest version's input position", () => {
  // newest-first input → chains stay newest-first by their tip.
  const items = [
    it("b2", "b1", "2026-02-02"), // chain B newest
    it("a1", null, "2026-01-01"), // chain A
    it("b1", null, "2026-02-01"),
  ];
  const chains = D.buildVersionChains(items);
  assert.deepEqual(chains.map((c) => c.root.id), ["b1", "a1"]);
});

test("buildVersionChains tolerates a parent_id cycle without looping forever", () => {
  // Defensive: a self/loop reference must not hang the grouping.
  const items = [
    { id: "x", parent_id: "y", created_at: "1" },
    { id: "y", parent_id: "x", created_at: "2" },
  ];
  const chains = D.buildVersionChains(items); // must return, not hang
  assert.ok(Array.isArray(chains));
});

test("buildVersionChains handles empty / non-array input", () => {
  assert.deepEqual(D.buildVersionChains([]), []);
  assert.deepEqual(D.buildVersionChains(null), []);
  assert.deepEqual(D.buildVersionChains(undefined), []);
});

/* ── URL <-> location (the refresh/nav fix) ─────────────────────────────── */

test("readLocationFromQuery parses subject/area/imageType", () => {
  assert.deepEqual(
    D.readLocationFromQuery("?subject=fysik&area=ljud&imageType=diagram"),
    { subject: "fysik", area: "ljud", imageType: "diagram" });
  // works without a leading '?'
  assert.deepEqual(
    D.readLocationFromQuery("subject=kemi"),
    { subject: "kemi", area: null, imageType: null });
});

test("readLocationFromQuery ignores area without a subject (areas are subject-scoped)", () => {
  assert.deepEqual(
    D.readLocationFromQuery("?area=ljud"),
    { subject: null, area: null, imageType: null });
});

test("readLocationFromQuery on empty/garbage is all-null", () => {
  for (const s of ["", "?", null, undefined, 123]) {
    assert.deepEqual(D.readLocationFromQuery(s), { subject: null, area: null, imageType: null });
  }
});

test("bibliotekUrl builds the restorable URL, encoding each id", () => {
  assert.equal(D.bibliotekUrl(null), "/bibliotek");
  assert.equal(D.bibliotekUrl("fysik"), "/bibliotek?subject=fysik");
  assert.equal(D.bibliotekUrl("fysik", "ljud"), "/bibliotek?subject=fysik&area=ljud");
  assert.equal(D.bibliotekUrl("fysik", "ljud", "diagram"),
    "/bibliotek?subject=fysik&area=ljud&imageType=diagram");
  // area only ever rides with a subject
  assert.equal(D.bibliotekUrl(null, "ljud"), "/bibliotek");
});

test("URL round-trips: read(build(x)) === x for a refresh-restore", () => {
  const url = D.bibliotekUrl("fysik", "ljud", "diagram");
  const search = url.slice(url.indexOf("?"));
  assert.deepEqual(D.readLocationFromQuery(search),
    { subject: "fysik", area: "ljud", imageType: "diagram" });
});
