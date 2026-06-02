/* Tests for the Bibliotek kind→media + level-label + kind-filter logic (lb-lesson).
 *
 * Runs under Node's built-in test runner (zero deps, no build step):
 *     node --test tests/bibliotek_kind.test.mjs
 *
 * The lb-lesson feature adds two VIDEO kinds — 'lesson' (a per-delområde overview)
 * and 'short' (a per-begrepp/family short) — alongside the P6 'film'. The Bibliotek
 * view must (a) render those with a <video> + a play badge (not as an <img>), (b)
 * tag a lesson/short card with its school level, and (c) offer a "Lektioner" filter
 * that groups lesson+short. These are the pure functions the view uses, kept in
 * LOCKSTEP with static/js/bibliotek.jsx (isVideoKind / levelLabel / the "lektion"
 * filter predicate) so the kind logic is pinned without a DOM/Babel runtime — the
 * same approach as tests/bibliotek_gallery.test.mjs::assetThumbSrc.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

/* In lockstep with bibliotek.jsx::isVideoKind. */
function isVideoKind(kind) {
  return kind === "film" || kind === "lesson" || kind === "short";
}
/* In lockstep with bibliotek.jsx::levelLabel. */
function levelLabel(level) {
  return ({ enkel: "Enkel", utvecklad: "Utvecklad", valutvecklad: "Välutvecklad" })[level] || null;
}
/* In lockstep with bibliotek.jsx's kind-filter predicate. */
function passesKindFilter(item, kind) {
  if (kind === "lektion") return item.kind === "lesson" || item.kind === "short";
  if (kind === "all") return true;
  return item.kind === kind;
}

test("isVideoKind: film + lesson + short are video; bild is not", () => {
  assert.equal(isVideoKind("film"), true);
  assert.equal(isVideoKind("lesson"), true);
  assert.equal(isVideoKind("short"), true);
  assert.equal(isVideoKind("bild"), false);
  assert.equal(isVideoKind(undefined), false);
});

test("levelLabel: the three school levels render their Swedish label; else null", () => {
  assert.equal(levelLabel("enkel"), "Enkel");
  assert.equal(levelLabel("utvecklad"), "Utvecklad");
  assert.equal(levelLabel("valutvecklad"), "Välutvecklad");
  assert.equal(levelLabel(null), null);
  assert.equal(levelLabel(undefined), null);
  assert.equal(levelLabel("expert"), null);
});

test("kind filter: 'all' passes everything", () => {
  for (const k of ["bild", "film", "lesson", "short"]) {
    assert.equal(passesKindFilter({ kind: k }, "all"), true);
  }
});

test("kind filter: exact kinds match only themselves", () => {
  assert.equal(passesKindFilter({ kind: "bild" }, "bild"), true);
  assert.equal(passesKindFilter({ kind: "film" }, "bild"), false);
  assert.equal(passesKindFilter({ kind: "film" }, "film"), true);
});

test("kind filter: 'lektion' groups lesson + short, excludes bild/film", () => {
  assert.equal(passesKindFilter({ kind: "lesson" }, "lektion"), true);
  assert.equal(passesKindFilter({ kind: "short" }, "lektion"), true);
  assert.equal(passesKindFilter({ kind: "film" }, "lektion"), false);
  assert.equal(passesKindFilter({ kind: "bild" }, "lektion"), false);
});

test("a lesson item filtered to its level-grouped tab still renders as video", () => {
  const item = { kind: "lesson", level: "enkel" };
  assert.equal(passesKindFilter(item, "lektion"), true);
  assert.equal(isVideoKind(item.kind), true);
  assert.equal(levelLabel(item.level), "Enkel");
});
