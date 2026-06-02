"""Tests for the media-serving route (lb-gallery).

Acceptance:
  * GET /media/<file_path> serves the bytes of a file under STORAGE_DIR with the
    correct Content-Type (PNG/JPEG/MP4), so the Bibliotek gallery <img>/<video>
    actually loads.
  * It is path-traversal-safe: a ``..`` escape, an absolute path, or a symlink
    pointing outside STORAGE_DIR all 404 (never serve, never 500, never leak).
  * An unknown file (or a directory) is a 404.

These are the safety-critical tests: this is the one route that maps a
client-supplied path onto the filesystem.
"""
from __future__ import annotations

import os

import pytest

from app import create_app


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def _app(storage_dir):
    # INIT_DB off — the media route doesn't touch the DB, so a temp storage dir
    # is all it needs. TESTING off-reconcile is implied by INIT_DB=False.
    return create_app({"TESTING": True, "INIT_DB": False, "STORAGE_DIR": storage_dir})


def _write(storage_dir, rel, data=PNG_BYTES):
    p = storage_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ── serves real files with the right content-type ──────────────────────────


def test_serves_png_with_correct_mime(tmp_path):
    storage = tmp_path / "storage"
    _write(storage, "output/fysik/rorelse/diagram/larbild_00001_.png")
    app = _app(storage)

    resp = app.test_client().get("/media/output/fysik/rorelse/diagram/larbild_00001_.png")
    assert resp.status_code == 200
    assert resp.data == PNG_BYTES
    assert resp.headers["Content-Type"].startswith("image/png")


def test_serves_jpeg_and_mp4_mimes(tmp_path):
    storage = tmp_path / "storage"
    _write(storage, "output/kemi/materia/foto/x.jpg", b"\xff\xd8\xff\xe0jpegdata")
    _write(storage, "output/biologi/celler/film/clip.mp4", b"\x00\x00\x00\x18ftypmp42")
    c = _app(storage).test_client()

    assert c.get("/media/output/kemi/materia/foto/x.jpg").headers["Content-Type"].startswith("image/jpeg")
    assert c.get("/media/output/biologi/celler/film/clip.mp4").headers["Content-Type"].startswith("video/mp4")


def test_unknown_extension_is_octet_stream(tmp_path):
    storage = tmp_path / "storage"
    _write(storage, "output/x/y/z/blob.unknownext", b"rawbytes")
    resp = _app(storage).test_client().get("/media/output/x/y/z/blob.unknownext")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/octet-stream")


# ── 404s ────────────────────────────────────────────────────────────────────


def test_unknown_file_is_404(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    resp = _app(storage).test_client().get("/media/output/does/not/exist.png")
    assert resp.status_code == 404


def test_directory_is_404(tmp_path):
    storage = tmp_path / "storage"
    _write(storage, "output/fysik/rorelse/diagram/a.png")
    # The directory itself must not be "served".
    resp = _app(storage).test_client().get("/media/output/fysik/rorelse/diagram")
    assert resp.status_code == 404


# ── path traversal safety (the security-critical cases) ────────────────────


def test_dotdot_traversal_is_blocked(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    # A secret OUTSIDE storage that must never be reachable.
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    c = _app(storage).test_client()

    # Various encodings of ../../secret.txt — all must 404, none may leak the file.
    for attack in (
        "/media/../secret.txt",
        "/media/output/../../secret.txt",
        "/media/output/../../../etc/passwd",
        "/media/..%2f..%2fsecret.txt",
        "/media/output/%2e%2e/%2e%2e/secret.txt",
    ):
        resp = c.get(attack)
        assert resp.status_code == 404, f"{attack} should 404, got {resp.status_code}"
        assert b"top secret" not in resp.data


def test_absolute_path_is_blocked(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    secret = tmp_path / "abs_secret.txt"
    secret.write_text("absolute secret")
    c = _app(storage).test_client()

    # The single-slash form a real client actually sends (no Werkzeug URL
    # normalization): /media/tmp/.../abs_secret.txt → handler runs → 404.
    abs_rel = str(secret).lstrip("/")
    resp = c.get(f"/media/{abs_rel}")
    assert resp.status_code == 404
    assert b"absolute secret" not in resp.data

    # The double-slash literal (/media//tmp/...) makes Werkzeug 308-redirect to
    # the normalized URL before our handler; follow it and assert it still never
    # serves the file. The invariant under test is "no leak", not a status code.
    followed = c.get(f"/media/{secret}", follow_redirects=True)
    assert b"absolute secret" not in followed.data
    assert followed.status_code == 404


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_symlink_escape_is_blocked(tmp_path):
    storage = tmp_path / "storage"
    (storage / "output").mkdir(parents=True)
    secret = tmp_path / "linked_secret.txt"
    secret.write_text("linked secret")
    # A symlink INSIDE storage pointing OUTSIDE it must not serve the target.
    link = storage / "output" / "escape.png"
    os.symlink(secret, link)

    resp = _app(storage).test_client().get("/media/output/escape.png")
    assert resp.status_code == 404
    assert b"linked secret" not in resp.data


def test_in_bounds_symlink_still_serves(tmp_path):
    # A symlink that stays inside STORAGE_DIR resolves fine (containment, not a
    # blanket symlink ban).
    storage = tmp_path / "storage"
    real = _write(storage, "output/real/a.png")
    link = storage / "output" / "alias.png"
    os.symlink(real, link)
    resp = _app(storage).test_client().get("/media/output/alias.png")
    assert resp.status_code == 200
    assert resp.data == PNG_BYTES
