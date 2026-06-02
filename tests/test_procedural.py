"""Unit tests for the procedural diagram engine (lb-proc).

The structured-content quality fix: for diagram / schema / tidslinje / molekyl /
reaction, a DETERMINISTIC renderer draws the exact structure + real Swedish
labels (graphviz / matplotlib / RDKit) instead of asking diffusion to guess. The
tests assert, per renderer:

  * a VALID PNG is produced (the magic bytes), and
  * the expected SWEDISH label strings actually reach the renderer (via the
    spec's ``all_labels()`` — the strings a vision check would read off the PNG).

graphviz/RDKit are exercised when their engine is present, and the matplotlib
FALLBACKS are forced + asserted too (so the engine is proven to render even on a
host without the ``dot`` binary or RDKit — the common Sibbis-first-boot case).

CPU-only, no GPU, no network — runs anywhere the rest of the suite runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import procedural as P

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _assert_png(path: Path) -> bytes:
    assert path.is_file(), f"no PNG written at {path}"
    data = path.read_bytes()
    assert data[:8] == _PNG_MAGIC, f"not a PNG: {path} starts {data[:8]!r}"
    assert len(data) > 200, "PNG suspiciously small"
    return data


# ── routing / kind mapping ──────────────────────────────────────────────────


def test_structured_image_types_route_procedurally():
    for t in ("diagram", "schema", "schematic", "tidslinje", "molekyl",
              "molecule", "reaktion", "reaction"):
        assert P.is_structured_image_type(t), t
    # diffusion types must NOT route here
    for t in ("foto", "illustration", "tecknad", "mikroskop", "tvarsnitt", "karta"):
        assert not P.is_structured_image_type(t), t
    assert not P.is_structured_image_type(None)
    assert not P.is_structured_image_type("")


def test_default_kind_for_picks_the_right_template():
    # kemi diagram → a molecule; fysik ljud/ljus → a labeled wave; fysik rörelse
    # → a force diagram; matematik → a plot; tidslinje → timeline; generic → block.
    assert P.default_kind_for("molekyl", "kemi", "x") == "molecule"
    assert P.default_kind_for("diagram", "kemi", "materia") == "molecule"
    assert P.default_kind_for("diagram", "fysik", "ljud") == "labeled-wave"
    assert P.default_kind_for("diagram", "fysik", "ljus") == "labeled-wave"
    assert P.default_kind_for("diagram", "fysik", "rorelse") == "force-diagram"
    assert P.default_kind_for("diagram", "matematik", "algebra") == "plot"
    assert P.default_kind_for("tidslinje", "historia", "x") == "timeline"
    assert P.default_kind_for("diagram", "biologi", "ekologi") == "block-diagram"


def test_engine_for_kind():
    assert P.engine_for_kind("block-diagram") == "graphviz"
    assert P.engine_for_kind("cycle") == "graphviz"
    assert P.engine_for_kind("labeled-wave") == "matplotlib"
    assert P.engine_for_kind("timeline") == "matplotlib"
    assert P.engine_for_kind("molecule") == "rdkit"
    with pytest.raises(P.ProceduralError):
        P.engine_for_kind("nonsense")


# ── spec validation ─────────────────────────────────────────────────────────


def test_validate_rejects_unknown_kind_and_missing_payload():
    with pytest.raises(P.ProceduralError):
        P.validate_spec({"kind": "wat"})
    with pytest.raises(P.ProceduralError):
        P.validate_spec({"kind": "block-diagram", "nodes": []})
    with pytest.raises(P.ProceduralError):
        P.validate_spec({"kind": "molecule", "smiles": ""})
    with pytest.raises(P.ProceduralError):
        P.validate_spec({"kind": "plot", "series": []})
    with pytest.raises(P.ProceduralError):
        P.validate_spec({"kind": "force-diagram", "forces": []})
    with pytest.raises(P.ProceduralError):
        P.validate_spec({"kind": "timeline", "events": []})


def test_spec_from_dict_ignores_unknown_keys():
    s = P.DiagramSpec.from_dict({"kind": "block-diagram", "nodes": ["A"],
                                 "totally_unknown": 123})
    assert s.kind == "block-diagram" and s.nodes == ["A"]
    assert s.to_dict()["kind"] == "block-diagram"


# ── the canonical block diagram: Inflöde → Process → Utflöde ────────────────


def test_block_diagram_inflode_process_utflode(tmp_path):
    spec = P.make_spec(subject="fysik", area="grundfysik", image_type="diagram",
                       begrepp="Fotosyntes", forklaring="Växter omvandlar ljus.")
    assert spec.kind == "block-diagram"
    labels = spec.all_labels()
    # the three pipeline boxes + the title, all Swedish
    assert "Inflöde" in labels
    assert "Utflöde" in labels
    assert any("Process" in l for l in labels)
    assert "Fotosyntes" in labels
    out = P.render_spec(spec, tmp_path / "block.png")
    _assert_png(out)


def test_block_diagram_matplotlib_fallback_when_no_dot(tmp_path, monkeypatch):
    # Force the no-`dot` path: the engine must still draw the labeled boxes via
    # matplotlib (the Sibbis-without-graphviz case).
    monkeypatch.setattr(P, "_have_dot", lambda: False)
    spec = P.make_spec(subject="kemi", area="x", image_type="diagram",
                       begrepp="Förbränning", kind="block-diagram")
    out = P.render_spec(spec, tmp_path / "block_fallback.png")
    _assert_png(out)


def test_explicit_edges_and_labels_render(tmp_path):
    spec = P.DiagramSpec(
        kind="flowchart", title="Vattenrening",
        nodes=[{"id": "a", "label": "Smutsigt vatten"},
               {"id": "b", "label": "Filter"},
               {"id": "c", "label": "Rent vatten"}],
        edges=[{"from": "a", "to": "b", "label": "leds in"},
               {"from": "b", "to": "c", "label": "renas"}],
    )
    labels = spec.all_labels()
    assert "Smutsigt vatten" in labels and "Rent vatten" in labels
    assert "leds in" in labels  # edge label carried
    _assert_png(P.render_spec(spec, tmp_path / "flow.png"))


# ── the labeled sound wave (våglängd / amplitud) ────────────────────────────


def test_labeled_wave_has_swedish_annotations(tmp_path):
    spec = P.make_spec(subject="fysik", area="ljud", image_type="diagram",
                       begrepp="Ljudvåg")
    assert spec.kind == "labeled-wave"
    # the wave renderer burns Våglängd (λ) + Amplitud + Swedish axes
    out = P.render_spec(spec, tmp_path / "wave.png")
    data = _assert_png(out)
    # axis labels come from the spec
    assert spec.x_label == "Avstånd (m)"
    assert spec.y_label == "Utslag"
    # a bigger render than an empty axes (text + curve actually drawn)
    assert len(data) > 5000


def test_wave_respects_custom_wave_params(tmp_path):
    spec = P.DiagramSpec(kind="labeled-wave", title="Ljus",
                         wave={"wavelength": 4.0, "amplitude": 2.0, "cycles": 1.5})
    _assert_png(P.render_spec(spec, tmp_path / "wave2.png"))


# ── force diagram ───────────────────────────────────────────────────────────


def test_force_diagram_labels_arrows(tmp_path):
    spec = P.make_spec(subject="fysik", area="rorelse", image_type="diagram",
                       begrepp="Låda")
    assert spec.kind == "force-diagram"
    labels = spec.all_labels()
    assert "Tyngdkraft" in labels and "Normalkraft" in labels
    _assert_png(P.render_spec(spec, tmp_path / "force.png"))


# ── plot + bar + timeline (matplotlib) ──────────────────────────────────────


def test_plot_renders_with_swedish_axes(tmp_path):
    spec = P.DiagramSpec(kind="plot", title="Hastighet", x_label="Tid (s)",
                         y_label="Sträcka (m)",
                         series=[{"label": "Bil", "x": [0, 1, 2], "y": [0, 5, 10]}])
    assert "Tid (s)" in spec.all_labels() and "Bil" in spec.all_labels()
    _assert_png(P.render_spec(spec, tmp_path / "plot.png"))


def test_bar_chart_renders(tmp_path):
    spec = P.DiagramSpec(kind="bar-chart", title="Nederbörd", x_label="Månad",
                         y_label="mm",
                         series=[{"label": "2024",
                                  "categories": ["Jan", "Feb", "Mar"],
                                  "values": [40, 30, 55]}])
    _assert_png(P.render_spec(spec, tmp_path / "bar.png"))


def test_timeline_orders_events(tmp_path):
    spec = P.make_spec(subject="historia", area="x", image_type="tidslinje",
                       begrepp="Industriella revolutionen")
    assert spec.kind == "timeline"
    assert any("Industriella" in l for l in spec.all_labels())
    _assert_png(P.render_spec(spec, tmp_path / "timeline.png"))


def test_timeline_multi_event(tmp_path):
    spec = P.DiagramSpec(kind="timeline", title="1900-talet", x_label="År",
                         events=[{"year": 1914, "label": "Första världskriget"},
                                 {"year": 1969, "label": "Månlandningen"},
                                 {"year": 1989, "label": "Berlinmuren faller"}])
    labels = spec.all_labels()
    assert "Månlandningen" in labels and "Berlinmuren faller" in labels
    _assert_png(P.render_spec(spec, tmp_path / "timeline2.png"))


# ── molecule (RDKit, + matplotlib fallback) ─────────────────────────────────


def test_molecule_water_from_lexicon(tmp_path):
    spec = P.make_spec(subject="kemi", area="materia", image_type="molekyl",
                       begrepp="Vatten")
    assert spec.kind == "molecule"
    assert spec.smiles == "O"           # water
    assert "Vatten" in spec.name        # Swedish name as legend
    _assert_png(P.render_spec(spec, tmp_path / "water.png"))


def test_molecule_ethanol_from_lexicon(tmp_path):
    spec = P.make_spec(subject="kemi", area="organisk", image_type="molekyl",
                       begrepp="Etanol")
    assert spec.smiles == "CCO"
    _assert_png(P.render_spec(spec, tmp_path / "ethanol.png"))


def test_unknown_molecule_defaults_to_water_label(tmp_path):
    # an unknown begrepp still yields a parseable SMILES + a label (never fails)
    smiles, name = P._concept_to_smiles("Något helt okänt")
    assert smiles == "O"
    assert name == "Något helt okänt"
    spec = P.make_spec(subject="kemi", area="x", image_type="molekyl",
                       begrepp="Något helt okänt")
    _assert_png(P.render_spec(spec, tmp_path / "unknown.png"))


def test_molecule_matplotlib_fallback_when_no_rdkit(tmp_path, monkeypatch):
    # Force the no-RDKit path: a molecule request must STILL produce a labeled PNG
    # (an atom/bond sketch), never fall through to a garbled diffusion render.
    monkeypatch.setattr(P, "_have_rdkit", lambda: False)
    spec = P.make_spec(subject="kemi", area="x", image_type="molekyl",
                       begrepp="Koldioxid")
    out = P.render_spec(spec, tmp_path / "co2_fallback.png")
    _assert_png(out)


def test_bad_smiles_raises_with_rdkit(tmp_path):
    if not P._have_rdkit():
        pytest.skip("RDKit not installed")
    with pytest.raises(P.ProceduralError):
        P.render_spec({"kind": "molecule", "smiles": "this-is-not-smiles!!!"},
                      tmp_path / "bad.png")


# ── concept-map + cycle ─────────────────────────────────────────────────────


def test_cycle_renders(tmp_path):
    spec = P.make_spec(subject="biologi", area="ekologi", image_type="diagram",
                       begrepp="Vattnets kretslopp", kind="cycle")
    assert spec.kind == "cycle"
    _assert_png(P.render_spec(spec, tmp_path / "cycle.png"))


def test_concept_map_renders(tmp_path):
    spec = P.make_spec(subject="biologi", area="celler", image_type="diagram",
                       begrepp="Cell", kind="concept-map")
    assert spec.kind == "concept-map"
    assert spec.center == "Cell"
    _assert_png(P.render_spec(spec, tmp_path / "map.png"))


# ── render_concept (the worker entry point) ─────────────────────────────────


def test_render_concept_default_uses_template(tmp_path):
    path, spec = P.render_concept(
        subject="fysik", area="grundfysik", image_type="diagram",
        begrepp="Avdunstning", forklaring="...", dest=tmp_path / "c.png",
    )
    _assert_png(path)
    assert spec.kind == "block-diagram"
    assert spec.begrepp == "Avdunstning"  # provenance carried onto the spec


def test_render_concept_with_explicit_spec(tmp_path):
    given = {"kind": "timeline", "title": "Min tidslinje",
             "events": [{"year": 2020, "label": "Start"}]}
    path, spec = P.render_concept(
        subject="historia", area="x", image_type="tidslinje", begrepp="X",
        dest=tmp_path / "c2.png", spec=given,
    )
    _assert_png(path)
    assert spec.kind == "timeline" and spec.title == "Min tidslinje"


# ── the LLM plug point: enrichment when it works, template when it doesn't ───


class _FakeLLM:
    """A stand-in composer: .post(...) returns an object whose .json() is canned."""

    def __init__(self, content: str | None = None, raise_on_post: bool = False):
        self.content = content
        self.raise_on_post = raise_on_post
        self.calls: list[dict] = []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - mirrors requests
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_on_post:
            raise RuntimeError("composer down")
        payload = {"choices": [{"message": {"content": self.content}}]}

        class _Resp:
            def json(self_inner):
                return payload
        return _Resp()


def test_spec_from_llm_uses_returned_json(tmp_path):
    # The composer returns a richer block diagram (real process steps); the plug
    # parses + validates it and we render it.
    content = (
        '```json\n'
        '{"kind":"block-diagram","title":"Fotosyntes",'
        '"nodes":["Koldioxid + Vatten","Klorofyll fångar ljus","Druvsocker + Syre"]}'
        '\n```'
    )
    llm = _FakeLLM(content=content)
    spec = P.spec_from_llm(subject="biologi", area="celler", image_type="diagram",
                           begrepp="Fotosyntes", forklaring="...", _client=llm)
    assert spec.kind == "block-diagram"
    assert "Klorofyll fångar ljus" in spec.all_labels()
    assert llm.calls  # the composer was actually called
    _assert_png(P.render_spec(spec, tmp_path / "llm.png"))


def test_spec_from_llm_falls_back_on_error():
    # composer down → the template spec is returned (a render is NEVER blocked).
    llm = _FakeLLM(raise_on_post=True)
    spec = P.spec_from_llm(subject="fysik", area="ljud", image_type="diagram",
                           begrepp="Ljudvåg", _client=llm)
    # the template for fysik/ljud is the labeled wave
    assert spec.kind == "labeled-wave"
    assert spec.begrepp == "Ljudvåg"


def test_spec_from_llm_falls_back_on_garbage():
    llm = _FakeLLM(content="sorry I can't do that")  # no JSON
    spec = P.spec_from_llm(subject="kemi", area="x", image_type="molekyl",
                           begrepp="Vatten", _client=llm)
    assert spec.kind == "molecule"  # fell back to the template (kemi molekyl)
    assert spec.smiles == "O"


def test_extract_json_handles_fences_and_prose():
    assert P._extract_json('```json\n{"a":1}\n```') == {"a": 1}
    assert P._extract_json('Here you go: {"b": 2} done') == {"b": 2}
    with pytest.raises(ValueError):
        P._extract_json("no object here")
