"""Smoke tests for the Lärbild Flask shell (lb-hd8, P0.1).

Verifies the acceptance criteria: the factory builds a runnable app, GET
/healthz returns 200 JSON ``{"status": "ok"}``, and the whole ``app/`` package
references zero NSFW modules — the SFW isolation contract from line one.
"""
from __future__ import annotations

import pathlib

from app import create_app


def test_factory_builds_app():
    app = create_app({"TESTING": True})
    assert app is not None
    assert app.config["TESTING"] is True
    # Canonical LAN port survives config load.
    assert app.config["PORT"] == 6767


def test_healthz_ok():
    app = create_app({"TESTING": True})
    client = app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.is_json
    assert resp.get_json() == {"status": "ok"}


def test_app_package_has_no_nsfw_references():
    """Every .py under app/ must be free of NSFW module/model references.

    A foundation guard only — P0.6 (lb-5sx) ships the authoritative CI check
    that also enforces repo isolation. This keeps the shell honest meanwhile.
    """
    markers = (
        "nsfw", "deepfake", "face_swap", "faceswap", "wardrobe",
        "lustify", "bigasp", "wai-nsfw", "wai_nsfw", "pony",
        "reactor", "insightface", "cyberrealistic", "porn",
    )
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    py_files = list(app_dir.rglob("*.py"))
    assert py_files, "no app/ python files found"
    for path in py_files:
        text = path.read_text(encoding="utf-8").lower()
        for marker in markers:
            assert marker not in text, f"NSFW marker {marker!r} found in {path}"
