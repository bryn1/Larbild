/* Tests for the Bibliotek gallery data logic (lb-gallery).
 *
 * Runs under Node's built-in test runner (zero deps, no build step):
 *     node --test tests/bibliotek_gallery.test.mjs
 *
 * Covers the pure pieces the gallery rendering relies on:
 *   • LBData.fmtDate — naive-UTC ISO → Europe/Stockholm display (timezone policy).
 *   • the thumbnail src resolution order the view's AssetCard uses
 *     (thumb_url → url → /media/<path>), reimplemented here as the same pure
 *     function so the precedence is pinned without a DOM/Babel runtime.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const D = require("../static/js/bibliotek_data.js");

/* The exact src-resolution rule AssetCard/Lightbox use (kept in lockstep with
 * static/js/bibliotek.jsx::assetThumbSrc — pure, so testable under node). */
function assetThumbSrc(item) {
  return item.thumb_url || item.url ||
    ((item.thumb_path || item.file_path) ? `/media/${item.thumb_path || item.file_path}` : null);
}

// ── fmtDate ────────────────────────────────────────────────────────────────

test("fmtDate renders a naive-UTC ISO in Europe/Stockholm", () => {
  // 2026-06-02T08:36:46 UTC → 10:36 Stockholm (CEST, UTC+2 in June).
  const out = D.fmtDate("2026-06-02T08:36:46");
  assert.match(out, /2026-06-02/);
  assert.match(out, /10:36/);
});

test("fmtDate respects an explicit Z (already UTC)", () => {
  const out = D.fmtDate("2026-01-15T12:00:00Z"); // winter → UTC+1 → 13:00
  assert.match(out, /2026-01-15/);
  assert.match(out, /13:00/);
});

test("fmtDate is safe on empty/garbage", () => {
  assert.equal(D.fmtDate(""), "");
  assert.equal(D.fmtDate(null), "");
  assert.equal(D.fmtDate("not-a-date"), "not-a-date");
});

// ── thumbnail src resolution ────────────────────────────────────────────────

test("assetThumbSrc prefers the API-provided thumb_url", () => {
  const item = { thumb_url: "/media/output/a/thumb.png", url: "/media/output/a/full.png" };
  assert.equal(assetThumbSrc(item), "/media/output/a/thumb.png");
});

test("assetThumbSrc falls back to url, then to building from file_path", () => {
  assert.equal(
    assetThumbSrc({ url: "/media/output/a/full.png" }),
    "/media/output/a/full.png"
  );
  assert.equal(
    assetThumbSrc({ file_path: "output/fysik/rorelse/diagram/larbild_00001_.png" }),
    "/media/output/fysik/rorelse/diagram/larbild_00001_.png"
  );
});

test("assetThumbSrc is null when nothing identifies an image", () => {
  assert.equal(assetThumbSrc({ title: "no image" }), null);
});
