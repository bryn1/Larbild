"""Tests for the Bibliotek frontend serving (lb-eee).

Acceptance (the server-side slice of it): the Bibliotek view is served at the
app root and the design-system CSS + the view's JS are reachable under /static,
so the no-build React page actually loads. The page must reference the live read
endpoints (/api/library + /api/taxonomy) and the design-system stylesheet — the
wiring the view depends on. The 3-level rendering itself (subject → area →
items + Centralt-innehåll) is exercised by the Node tests over the pure data
layer (tests/bibliotek_data.test.mjs).
"""
from __future__ import annotations

from app import create_app


def _client():
    return create_app({"TESTING": True, "INIT_DB": False}).test_client()


# ── the SPA shell is served ────────────────────────────────────────────────


def test_index_serves_html():
    resp = _client().get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    assert b'<div id="root">' in resp.data


def test_bibliotek_route_serves_same_shell():
    resp = _client().get("/bibliotek")
    assert resp.status_code == 200
    assert b'<div id="root">' in resp.data


def test_index_references_live_endpoints_and_design_css():
    html = _client().get("/").get_data(as_text=True)
    # the view bootstraps from the real backend, not a bundled data.js mock
    assert "/static/js/bibliotek.jsx" in html
    assert "/static/js/bibliotek_data.js" in html
    # design system stylesheet (tokens → base → components entry point)
    assert "/static/css/larbild.css" in html
    # the three design fonts
    assert "Fraunces" in html and "Schibsted+Grotesk" in html and "Space+Mono" in html


# ── /static actually serves the repo-root assets the page loads ────────────


def test_static_css_is_served():
    resp = _client().get("/static/css/larbild.css")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # the entry point imports the three layers in order
    assert "tokens.css" in body and "base.css" in body and "components.css" in body


def test_static_js_view_and_data_served():
    client = _client()
    data_js = client.get("/static/js/bibliotek_data.js")
    assert data_js.status_code == 200
    assert "orderSubjects" in data_js.get_data(as_text=True)

    view_js = client.get("/static/js/bibliotek.jsx")
    assert view_js.status_code == 200
    src = view_js.get_data(as_text=True)
    # the view fetches the live read endpoints
    assert "/api/library" in src
    assert "/api/taxonomy" in src


def test_no_skapa_funnel_link_remains():
    """The Skapa generate funnel + its deep-link were removed with the diffusion
    render path. The Bibliotek shell must no longer reference them."""
    client = _client()
    data_js = client.get("/static/js/bibliotek_data.js").get_data(as_text=True)
    view_js = client.get("/static/js/bibliotek.jsx").get_data(as_text=True)
    assert "skapaLink" not in data_js
    assert "skapaLink" not in view_js
    assert "/skapa" not in view_js


def test_view_js_has_no_nsfw_marker():
    """The frontend JS must also stay SFW (mirrors the app-package guard)."""
    markers = (
        "nsfw", "deepfake", "face_swap", "faceswap", "wardrobe", "lustify",
        "bigasp", "wai-nsfw", "wai_nsfw", "pony", "reactor", "insightface",
        "cyberrealistic", "porn",
    )
    client = _client()
    for path in ("/static/js/bibliotek.jsx", "/static/js/bibliotek_data.js", "/static/index.html"):
        text = client.get(path).get_data(as_text=True).lower()
        for m in markers:
            assert m not in text, f"marker {m!r} found in {path}"
