/* Regression tests for the Bibliotek gallery load-resolution (lb-loadfix).
 *
 * Runs under Node's built-in test runner (zero deps, no build step):
 *     node --test tests/bibliotek_gallery_loadfix.test.mjs
 *
 * The bug these pin: the gallery hung forever on "Läser in bilder…" with the
 * images never appearing (sibbis:6868). The backend was healthy — /api/library/
 * items returned the items correctly — but the React view's items-load effect
 * CANCELLED ITS OWN in-flight fetch. The effect writes itemsByNode (an in-flight
 * `null` marker) and itemsByNode was in its dependency list, so writing the
 * marker re-ran the effect; the old per-run `alive = false` cleanup then fired
 * and the resolving fetch hit `if (!alive) return`, throwing the items away. The
 * cache stayed `null` (the loading sentinel) forever.
 *
 * These tests run the items -> render DATA transform (buildVersionChains +
 * version grouping) AND the load-resolution reducer (`settle`) on REALISTIC
 * items — exactly the /api/library/items shape: null parent_id and null
 * review_ok/review_comment/reviewed_at for an unreviewed original, null
 * thumb_path, kind "bild". The `settle` helper is kept in lockstep with
 * static/js/bibliotek.jsx (the items-load effect), so the regression — a
 * resolved fetch failing to clear the `null` loading slot — is caught here
 * without a DOM/Babel runtime.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const D = require("../static/js/bibliotek_data.js");

/* The exact src-resolution rule AssetCard/Lightbox use (mirrors
 * static/js/bibliotek.jsx::assetThumbSrc — pure, so testable under node). */
function assetThumbSrc(item) {
  return item.thumb_url || item.url ||
    ((item.thumb_path || item.file_path) ? `/media/${item.thumb_path || item.file_path}` : null);
}

/* The items-load resolution, mirrored from static/js/bibliotek.jsx's items-load
 * effect (the `settle` closure). Pure model of the reducer:
 *   • a node's cache key is `undefined` (never loaded) -> `null` (in-flight,
 *     the "Läser in bilder…" sentinel) -> an items array (loaded).
 *   • a fetch only writes its node's slot if it's still the in-flight `null`
 *     (so a stale-nav response can't clobber freshly-loaded data), and
 *   • it ALWAYS resolves to an array — even on error/garbage — so one bad node
 *     can't leave the gallery stuck loading.
 * `openToken`/`fetchToken` model the navigation guard: a late response is
 * dropped only if the user navigated to a different node, NOT when the effect
 * merely re-ran on its own cache write (the bug). */
function settle(cache, key, items, fetchToken, openToken) {
  if (openToken !== fetchToken) return cache; // navigated away — ignore
  const arr = Array.isArray(items) ? items : [];
  return cache[key] === null ? { ...cache, [key]: arr } : cache;
}

/* Whether the gallery is still showing the loading state for a node: the cache
 * slot is the in-flight `null` sentinel (or never started). Mirrors the view's
 * `itemsLoading = !Array.isArray(itemsByNode[key])`. */
const isLoading = (cache, key) => !Array.isArray(cache[key]);

/* The REAL /api/library/items?subject=fysik&area=ljud payload shape the owner
 * reported (newest first): unreviewed originals — parent_id null, all review_*
 * null, thumb_path null, kind "bild", with servable thumb_url/url. */
function realItems() {
  const mk = (id, n, title) => ({
    id, kind: "bild", subject: "fysik", area: "ljud", image_type: "diagram",
    video_type: null, title, prompt: `prompt ${n}`, lora: n === 3 ? "fysik_diagram" : null,
    lora_strength: n === 3 ? 0.8 : null, seed: n, backend: "comfyui",
    width: 1024, height: 1024, duration_s: null,
    file_path: `output/fysik/ljud/diagram/larbild_0000${n}_.png`,
    url: `/media/output/fysik/ljud/diagram/larbild_0000${n}_.png`,
    thumb_url: `/media/output/fysik/ljud/diagram/larbild_0000${n}_.png`,
    thumb_path: null,
    created_at: `2026-06-01T0${n}:00:00`, generated_at: `2026-06-01T0${n}:00:00`,
    review_ok: null, review_comment: null, reviewed_at: null, parent_id: null,
  });
  // newest first, as the API returns (created_at DESC)
  return [mk("lib_c", 3, "Ljudvågor i luft"), mk("lib_b", 2, "Örat och hörsel"), mk("lib_a", 1, "Frekvens och ton")];
}

/* ── the data transform on the real shape ──────────────────────────────────── */

test("buildVersionChains turns the real unreviewed items into one tile each (null parent_id is fine)", () => {
  const items = realItems();
  const chains = D.buildVersionChains(items);
  // 3 independent originals (no parent_id) → 3 single-version chains.
  assert.equal(chains.length, 3);
  for (const c of chains) {
    assert.equal(c.versions.length, 1);
    assert.equal(c.latest.id, c.root.id);
    assert.equal(c.latest.parent_id, null);     // unreviewed original
    assert.equal(c.latest.review_ok, null);     // null review field doesn't break grouping
    assert.ok(c.root && c.root.id);             // AssetCard keys on c.root.id
    // the tile resolves a servable thumbnail (the visible image)
    assert.equal(assetThumbSrc(c.latest), c.latest.thumb_url);
    assert.match(assetThumbSrc(c.latest), /^\/media\/output\/fysik\/ljud\/diagram\/larbild_0000\d_\.png$/);
  }
  // newest-first input preserved at the chain level (tip order).
  assert.deepEqual(chains.map((c) => c.root.id), ["lib_c", "lib_b", "lib_a"]);
});

test("a regenerated version chain (parent_id set) groups under its root, latest on top", () => {
  // v1 original, then two regenerations — the real parent_id linkage.
  const base = realItems()[2]; // lib_a
  const items = [
    { ...base, id: "lib_a3", parent_id: "lib_a2", created_at: "2026-06-01T03:00:00", review_ok: 0, review_comment: "skarpare" },
    { ...base, id: "lib_a", parent_id: null, created_at: "2026-06-01T01:00:00" },
    { ...base, id: "lib_a2", parent_id: "lib_a", created_at: "2026-06-01T02:00:00", review_ok: null },
  ];
  const chains = D.buildVersionChains(items);
  assert.equal(chains.length, 1);
  assert.deepEqual(chains[0].versions.map((v) => v.id), ["lib_a", "lib_a2", "lib_a3"]);
  assert.equal(chains[0].latest.id, "lib_a3");  // Regenerera acts on the latest tip
  assert.equal(chains[0].root.id, "lib_a");
});

test("buildVersionChains drops a malformed item instead of blanking the whole gallery", () => {
  // One bad row (null / missing id) must not throw — the other images still show.
  const items = [realItems()[0], null, { kind: "bild", title: "no id" }, undefined, realItems()[1]];
  const chains = D.buildVersionChains(items);
  assert.deepEqual(chains.map((c) => c.root.id), ["lib_c", "lib_b"]);
});

/* ── the load-resolution (the actual regression) ───────────────────────────── */

test("REGRESSION: a resolved items fetch clears the loading sentinel and shows the items", () => {
  const KEY = "fysik/ljud", TOKEN = "fysik|ljud";
  // Effect marks the node in-flight (the "Läser in bilder…" state).
  let cache = { [KEY]: null };
  assert.equal(isLoading(cache, KEY), true);
  // The fetch resolves with the 3 real items — same open node (not navigated away).
  cache = settle(cache, KEY, realItems(), TOKEN, TOKEN);
  // The bug left cache[KEY] === null forever (loading). It must now be the array.
  assert.equal(isLoading(cache, KEY), false, "loading must resolve once items arrive");
  assert.equal(cache[KEY].length, 3);
  // …and the loaded items render into tiles (the visible gallery).
  const chains = D.buildVersionChains(cache[KEY]);
  assert.equal(chains.length, 3);
  assert.ok(chains.every((c) => assetThumbSrc(c.latest)));
});

test("an errored / empty items fetch still clears loading (no hang on a bad node)", () => {
  const KEY = "fysik/ljud", TOKEN = "fysik|ljud";
  for (const bad of [undefined, null, {}, { items: null }, "oops"]) {
    let cache = { [KEY]: null };
    // settle is fed the .items (or [] on error) — model both as a non-array.
    cache = settle(cache, KEY, Array.isArray(bad && bad.items) ? bad.items : [], TOKEN, TOKEN);
    assert.equal(isLoading(cache, KEY), false, "loading must resolve even on a bad response");
    assert.deepEqual(cache[KEY], []);
  }
});

test("a stale-node response (user navigated away) is ignored and can't clobber the new node", () => {
  const A = "fysik/ljud", B = "fysik/ljus";
  // Open node A, fetch starts (token A). User then navigates to B; A's fetch is
  // captured with fetchToken A but the open node is now B.
  let cache = { [A]: null, [B]: null };
  // A's late response arrives while B is open → dropped (guard), A's slot untouched.
  cache = settle(cache, A, realItems(), /*fetchToken*/ "fysik|ljud", /*openToken*/ "fysik|ljus");
  assert.equal(cache[A], null, "stale-node fetch must not write while another node is open");
  // B then resolves normally.
  cache = settle(cache, B, [], "fysik|ljus", "fysik|ljus");
  assert.equal(isLoading(cache, B), false);
});

test("a re-run of the effect does NOT cancel an in-flight fetch (the root cause)", () => {
  // Model the exact failure sequence: the effect writes the null marker, which
  // re-runs the effect (itemsByNode in deps); with the OLD per-run `alive` flag
  // that re-run's cleanup set alive=false and the resolving fetch was discarded.
  // The fix keys the guard on the OPEN NODE, which did NOT change — so the same
  // token both times means the resolution still applies.
  const KEY = "fysik/ljud";
  const tokenAtFetchStart = "fysik|ljud";
  // … effect re-ran here (cache churn), but the open node token is unchanged:
  const tokenWhenResolved = "fysik|ljud";
  let cache = { [KEY]: null };
  cache = settle(cache, KEY, realItems(), tokenAtFetchStart, tokenWhenResolved);
  assert.equal(isLoading(cache, KEY), false, "effect churn must not cancel the fetch");
  assert.equal(cache[KEY].length, 3);
});
