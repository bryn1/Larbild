"""Tests for the Skapa-bild funnel serving (lb-e1p / P3.1).

Acceptance (the server-side slice): the funnel page is served at /skapa and its
no-build React scripts (skapa_data.js + skapa.jsx) are reachable under /static,
so the page actually loads. The page must reference the live SoT endpoints it
bootstraps from (/api/taxonomy, /api/resolve-lora) and the design-system CSS.
The funnel/payload logic itself is exercised by the Node tests over the pure
data layer (tests/skapa_data.test.mjs).

The SFW-marker guard (mirroring test_bibliotek_view) is extended to the new
funnel files — the funnel is strictly SFW and must carry no restricted marker
(notably: the design's face-swap "Ansikte" section is intentionally omitted).
"""
from __future__ import annotations

from app import create_app


def _client():
    return create_app({"TESTING": True, "INIT_DB": False}).test_client()


# ── the funnel shell is served ─────────────────────────────────────────────


def test_skapa_route_serves_html():
    resp = _client().get("/skapa")
    assert resp.status_code == 200
    assert "text/html" in resp.content_type
    assert b'<div id="root">' in resp.data


def test_skapa_html_references_funnel_scripts_and_design_css():
    html = _client().get("/skapa").get_data(as_text=True)
    # the funnel loads its pure data layer then the React view
    assert "/static/js/skapa_data.js" in html
    assert "/static/js/skapa.jsx" in html
    # design system stylesheet + the three design fonts
    assert "/static/css/larbild.css" in html
    assert "Fraunces" in html and "Schibsted+Grotesk" in html and "Space+Mono" in html


# ── /static actually serves the funnel assets the page loads ───────────────


def test_static_funnel_js_served_and_wired_to_live_endpoints():
    client = _client()
    data_js = client.get("/static/js/skapa_data.js")
    assert data_js.status_code == 200
    assert "buildJobPayload" in data_js.get_data(as_text=True)

    view_js = client.get("/static/js/skapa.jsx")
    assert view_js.status_code == 200
    src = view_js.get_data(as_text=True)
    # the funnel bootstraps from the taxonomy SoT and resolves the LoRA server-side
    assert "/api/taxonomy" in src
    assert "/api/resolve-lora" in src


def test_funnel_js_has_no_nsfw_marker():
    """The funnel JS + shell must stay SFW (mirrors the bibliotek marker guard).

    Includes face-swap markers on purpose: the design's "Ansikte" section is
    deliberately NOT ported into this SFW funnel, so none of these may appear.
    """
    markers = (
        "nsfw", "deepfake", "face_swap", "faceswap", "wardrobe", "lustify",
        "bigasp", "wai-nsfw", "wai_nsfw", "pony", "reactor", "insightface",
        "cyberrealistic", "porn",
    )
    client = _client()
    for path in ("/static/js/skapa.jsx", "/static/js/skapa_data.js", "/static/skapa.html"):
        text = client.get(path).get_data(as_text=True).lower()
        for m in markers:
            assert m not in text, f"marker {m!r} found in {path}"
