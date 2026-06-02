"""Tests for the review verdict + regenerate API (lb-review-regen).

End-to-end through the Flask app with a FakeRedis (no live GPU/network):

  * POST /api/library/<id>/review sets/clears the OK / Inte-OK verdict + a note,
    partial-update (toggling the verdict never wipes the comment), 404 on
    unknown/deleted, 400 on a junk verdict.
  * POST /api/library/<id>/regenerate enqueues a NEW render job for the same
    subject/area/imageType, links it to the source via parent_id, folds the
    note into the prompt, returns 202; 400 for a film, 404 for unknown, 429 when
    the queue is full.
  * GET /api/library/items carries the review state + parent_id per item and
    supports ?review=pending|ok|notok filtering.

The regenerate→child library row link (the worker copies jobs.parent_id onto the
produced library row) is exercised by driving the worker's _autosort_to_library
directly with the enqueued job, proving the version chain survives the async hop.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from app import create_app, jobs
from app.db import connect
from app.render_worker import _autosort_to_library
from tests._fakeredis import FakeRedis


def _app(tmp_path):
    return create_app({"TESTING": True, "DB_PATH": tmp_path / "rr.db"})


def _fresh_redis():
    r = FakeRedis()
    jobs.set_redis(r)
    return r


def _insert(db_path, *, subject="fysik", area="ljud", image_type="diagram",
            kind="bild", title="Bild", prompt="a transverse wave on a string",
            file_path=None, deleted=False, parent_id=None, review_ok=None,
            review_comment=None, created_at=None):
    rid = str(uuid.uuid4())
    fp = file_path or f"output/{subject}/{area}/{image_type}/{rid}.png"
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library
                (id, kind, subject, area, image_type, video_type, title, file_path,
                 prompt, parent_id, review_ok, review_comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            """,
            (rid, kind, subject, area,
             image_type if kind == "bild" else None,
             None if kind == "bild" else image_type,
             title, fp, prompt, parent_id, review_ok, review_comment, created_at),
        )
        if deleted:
            conn.execute("UPDATE library SET deleted_at = datetime('now') WHERE id = ?", (rid,))
    return rid, fp


# ── REVIEW endpoint ─────────────────────────────────────────────────────────


def test_review_sets_verdict_and_comment(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        rid, _ = _insert(tmp_path / "rr.db")
        resp = app.test_client().post(
            f"/api/library/{rid}/review",
            json={"review_ok": 0, "comment": "wavelength too short"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["review_ok"] == 0
        assert body["review_comment"] == "wavelength too short"
        assert body["reviewed_at"]  # stamped
        with connect(tmp_path / "rr.db") as conn:
            row = conn.execute("SELECT review_ok, review_comment FROM library WHERE id = ?", (rid,)).fetchone()
        assert row["review_ok"] == 0 and row["review_comment"] == "wavelength too short"
    finally:
        jobs.set_redis(None)


def test_review_accepts_ok_and_bool_and_string(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        for payload, expect in (
            ({"review_ok": 1}, 1),
            ({"review_ok": True}, 1),
            ({"review_ok": "ok"}, 1),
            ({"review_ok": 0}, 0),
            ({"review_ok": False}, 0),
            ({"review_ok": "notok"}, 0),
        ):
            rid, _ = _insert(tmp_path / "rr.db")
            body = c.post(f"/api/library/{rid}/review", json=payload).get_json()
            assert body["review_ok"] == expect, payload
    finally:
        jobs.set_redis(None)


def test_review_partial_update_keeps_other_field(tmp_path):
    # Toggling the verdict must NOT wipe an existing comment (atelier semantics).
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        rid, _ = _insert(tmp_path / "rr.db")
        c.post(f"/api/library/{rid}/review", json={"comment": "needs a label"})
        body = c.post(f"/api/library/{rid}/review", json={"review_ok": 0}).get_json()
        assert body["review_ok"] == 0
        assert body["review_comment"] == "needs a label"  # preserved
    finally:
        jobs.set_redis(None)


def test_review_clears_verdict_with_null(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        c = app.test_client()
        rid, _ = _insert(tmp_path / "rr.db", review_ok=1)
        body = c.post(f"/api/library/{rid}/review", json={"review_ok": None}).get_json()
        assert body["review_ok"] is None
    finally:
        jobs.set_redis(None)


def test_review_rejects_bad_verdict(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        rid, _ = _insert(tmp_path / "rr.db")
        resp = app.test_client().post(f"/api/library/{rid}/review", json={"review_ok": "maybe"})
        assert resp.status_code == 400
    finally:
        jobs.set_redis(None)


def test_review_empty_body_is_400(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        rid, _ = _insert(tmp_path / "rr.db")
        assert app.test_client().post(f"/api/library/{rid}/review", json={}).status_code == 400
    finally:
        jobs.set_redis(None)


def test_review_unknown_id_is_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        assert app.test_client().post("/api/library/nope/review", json={"review_ok": 1}).status_code == 404
    finally:
        jobs.set_redis(None)


def test_review_soft_deleted_is_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        rid, _ = _insert(tmp_path / "rr.db", deleted=True)
        assert app.test_client().post(f"/api/library/{rid}/review", json={"review_ok": 1}).status_code == 404
    finally:
        jobs.set_redis(None)


# ── items: review state + filter ────────────────────────────────────────────


def test_items_carry_review_state_and_parent(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        rid, _ = _insert(tmp_path / "rr.db", review_ok=1, review_comment="bra")
        item = app.test_client().get(
            "/api/library/items?subject=fysik&area=ljud"
        ).get_json()["items"][0]
        for key in ("review_ok", "review_comment", "reviewed_at", "parent_id"):
            assert key in item, f"item missing {key}"
        assert item["review_ok"] == 1 and item["review_comment"] == "bra"
        assert item["parent_id"] is None
    finally:
        jobs.set_redis(None)


def test_items_review_filter_pending_ok_notok(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        _insert(db, area="ljud", review_ok=None)   # pending
        _insert(db, area="ljud", review_ok=1)       # ok
        _insert(db, area="ljud", review_ok=0)       # notok
        c = app.test_client()

        def count(rev):
            return c.get(f"/api/library/items?subject=fysik&area=ljud&review={rev}").get_json()["count"]

        assert count("pending") == 1
        assert count("ok") == 1
        assert count("notok") == 1
        # no filter → all three; an unknown filter value also yields all (no filter)
        all_body = c.get("/api/library/items?subject=fysik&area=ljud").get_json()
        assert all_body["count"] == 3 and all_body["review"] is None
        bogus = c.get("/api/library/items?subject=fysik&area=ljud&review=weird").get_json()
        assert bogus["count"] == 3 and bogus["review"] is None
    finally:
        jobs.set_redis(None)


# ── REGENERATE endpoint ─────────────────────────────────────────────────────


def test_regenerate_enqueues_linked_child_job(tmp_path):
    r = _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        rid, _ = _insert(db, subject="fysik", area="ljud", image_type="diagram",
                         prompt="a transverse wave")
        resp = app.test_client().post(
            f"/api/library/{rid}/regenerate",
            json={"comment": "longer wavelength, label crest and trough"},
        )
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["status"] == "queued"
        assert body["parent_id"] == rid
        job_id = body["job_id"]
        # the enqueued job carries the parent link + the same taxonomy keys + the
        # guided prompt (original + the correction note).
        with connect(db) as conn:
            j = conn.execute(
                "SELECT kind, subject, area, image_type, prompt, parent_id "
                "FROM jobs WHERE id = ?", (job_id,),
            ).fetchone()
        assert j["kind"] == "bild"
        assert (j["subject"], j["area"], j["image_type"]) == ("fysik", "ljud", "diagram")
        assert j["parent_id"] == rid
        assert "a transverse wave" in j["prompt"]
        assert "longer wavelength" in j["prompt"]  # the note folded in
        # actually enqueued on the larbild queue
        assert r.llen("larbild:render:task_queue") == 1
    finally:
        jobs.set_redis(None)


def test_regenerate_uses_source_comment_when_none_given(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        rid, _ = _insert(db, prompt="base", review_comment="add a scale bar")
        body = app.test_client().post(f"/api/library/{rid}/regenerate", json={}).get_json()
        with connect(db) as conn:
            prompt = conn.execute("SELECT prompt FROM jobs WHERE id = ?", (body["job_id"],)).fetchone()["prompt"]
        assert "add a scale bar" in prompt  # the source verdict note guided it
    finally:
        jobs.set_redis(None)


def test_regenerate_fresh_comment_persisted_on_source(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        rid, _ = _insert(db, prompt="base")
        app.test_client().post(f"/api/library/{rid}/regenerate", json={"comment": "fix lighting"})
        with connect(db) as conn:
            row = conn.execute("SELECT review_comment FROM library WHERE id = ?", (rid,)).fetchone()
        assert row["review_comment"] == "fix lighting"  # recorded why it was redone
    finally:
        jobs.set_redis(None)


def test_regenerate_child_row_links_to_parent_through_worker(tmp_path):
    # The full chain: regenerate → job(parent_id) → worker auto-sorts a library
    # row that copies parent_id, so the new version is linked to the source.
    _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        rid, _ = _insert(db, prompt="base prompt")
        job_id = app.test_client().post(
            f"/api/library/{rid}/regenerate", json={"comment": "improve"}
        ).get_json()["job_id"]
        with app.app_context():
            job = jobs.get_job_row(job_id)
            meta = {"prompt": job["prompt"], "seed": 7, "width": 1024, "height": 768,
                    "lora": {}, "area": "ljud"}
            child_id = _autosort_to_library(job, meta, Path("fysik/ljud/diagram/child.png"))
        with connect(db) as conn:
            ch = conn.execute("SELECT parent_id FROM library WHERE id = ?", (child_id,)).fetchone()
        assert ch["parent_id"] == rid  # the version chain link survived the hop
        # and the child now shows up in the gallery alongside the original
        items = app.test_client().get("/api/library/items?subject=fysik&area=ljud").get_json()["items"]
        by_id = {it["id"]: it for it in items}
        assert by_id[child_id]["parent_id"] == rid
        assert by_id[rid]["parent_id"] is None
    finally:
        jobs.set_redis(None)


def test_regenerate_film_is_400(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        rid, _ = _insert(db, kind="film", image_type="forklaring",
                         file_path="output/fysik/ljud/film/clip.mp4")
        resp = app.test_client().post(f"/api/library/{rid}/regenerate", json={})
        assert resp.status_code == 400
    finally:
        jobs.set_redis(None)


def test_regenerate_unknown_is_404(tmp_path):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        assert app.test_client().post("/api/library/nope/regenerate", json={}).status_code == 404
    finally:
        jobs.set_redis(None)


def test_regenerate_queue_full_is_429(tmp_path, monkeypatch):
    _fresh_redis()
    try:
        app = _app(tmp_path)
        db = tmp_path / "rr.db"
        rid, _ = _insert(db)
        # Force the capacity check to report a full queue.
        monkeypatch.setattr(jobs, "queue_depth", lambda: jobs.MAX_DEPTH)
        resp = app.test_client().post(f"/api/library/{rid}/regenerate", json={})
        assert resp.status_code == 429
    finally:
        jobs.set_redis(None)
