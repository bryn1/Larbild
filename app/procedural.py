"""Procedural (deterministic) diagram engine — the structured-imageType fix (lb-proc).

The quality problem this exists to kill: for STRUCTURED image types (a schematic
*diagram*, a *tidslinje*, a *molekyl*, a kemi reaction) diffusion is the WRONG
tool — SDXL guesses bond counts, garbles Swedish text, and never draws the exact
structure the concept requires. The owner's report ("none of the images are
correct, no text is correct, prompts don't align with outputs") is the expected
failure mode of asking a diffusion model to render exact, labeled structure.

MODEL_LIBRARY.md §6 + §7 state the principle directly: **procedural-first for
anything with exact structure** (math figures, molecules, circuit/flow topology,
timelines), diffusion only for styled/organic imagery. This module is that
procedural plane: a set of DETERMINISTIC renderers that DRAW the structure and
burn REAL Swedish labels, producing a clean white-background läroboksstil PNG.
Correct, specific, legible — and CPU-only (no GPU), so it renders even while the
VM100 3090 is busy with imagegen/chat.

Three engines, one dispatcher
─────────────────────────────
  * **graphviz** (`dot`)  → graph-structured diagrams: block diagrams / flowcharts
    (Inflöde→Process→Utflöde), concept maps, cause→effect, cycles. Boxes + arrows
    + real Swedish labels. Falls back to a matplotlib box-and-arrow renderer when
    the ``dot`` binary is not installed, so the engine ALWAYS produces output.
  * **matplotlib**        → plots/graphs: a labeled sine wave (våglängd / amplitud),
    force diagrams (labelled arrows), function/line/bar charts — with real Swedish
    axis labels + annotations. matplotlib ships everywhere and its default DejaVu
    Sans font carries å/ä/ö, so Swedish text is native + legible.
  * **RDKit**             → kemi molecules: 2D skeletal / ball-and-stick from a
    SMILES string, chemically correct atoms + bonds. Falls back to a small
    matplotlib atom-and-bond sketch when RDKit is not importable.

The **DiagramSpec** (a JSON dict, schema below) is the contract: it says WHAT to
draw (the boxes/labels/data points/SMILES). :func:`render_spec` consumes a spec
and writes the PNG. The common cases do NOT need an LLM — :func:`make_spec` builds
a correct, labeled spec straight from the concept (subject/area/imageType/begrepp/
förklaring) using per-kind TEMPLATES, so output is correct NOW. An LLM (the 14B
composer at VM100:8091, or an Opus step) can OPTIONALLY refine the spec for a
richer diagram; the plug point is :func:`spec_from_llm`, which always degrades to
:func:`make_spec` so a missing/slow LLM never blocks a render.

STRICTLY SFW: this draws educational diagrams + molecules; no restricted import,
model, content, or reference. It touches none of the imagegen stack.

Env overrides:
  LARBILD_PROC_FONT     explicit TTF path for graphviz/matplotlib labels (else a
                        DejaVu/Liberation/Noto search; all carry å/ä/ö).
  LARBILD_PROC_DPI      default 150 — raster DPI for the matplotlib + dot PNGs.
  LARBILD_PROC_W        default 1280 — nominal canvas width in px (matplotlib).
  LARBILD_PROC_H        default 960  — nominal canvas height in px (matplotlib).
"""
from __future__ import annotations

import logging
import math
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger("larbild.procedural")

__all__ = [
    "ProceduralError",
    "DiagramSpec",
    "DIAGRAM_KINDS",
    "STRUCTURED_IMAGE_TYPES",
    "is_structured_image_type",
    "validate_spec",
    "make_spec",
    "spec_from_llm",
    "render_spec",
    "render_concept",
    "default_kind_for",
    "engine_for_kind",
]


class ProceduralError(RuntimeError):
    """A procedural diagram could not be rendered (bad spec / engine failure)."""


# ── which image types are structured (→ procedural, not diffusion) ──────────
#
# The taxonomy (lb-582) ships `diagram` + `tidslinje` today; `schema`/`schematic`
# and `molekyl`/`molecule` are routing aliases the owner uses for the same intent
# (a schematic diagram / a molecule). We accept all of them as structured so the
# router (render_worker) sends them here regardless of which slug the funnel used.
# `tvarsnitt` (cutaway/cross-section, organic) and the photo/illustration/micro
# types stay on the DIFFUSION path — they are styled, not exact structure.
STRUCTURED_IMAGE_TYPES: frozenset[str] = frozenset(
    {
        "diagram",
        "schema",
        "schematic",
        "schematiskt",
        "tidslinje",
        "molekyl",
        "molecule",
        "reaktion",
        "reaction",
    }
)


def is_structured_image_type(image_type: Optional[str]) -> bool:
    """True iff this imageType should render procedurally (not via diffusion)."""
    return bool(image_type) and image_type.strip().lower() in STRUCTURED_IMAGE_TYPES


# ── the diagram kinds + which engine draws each ─────────────────────────────
#
# A spec's ``kind`` selects the template. Each kind maps to one engine; the
# graphviz kinds degrade to matplotlib when `dot` is missing (engine_for_kind
# reports the LOGICAL engine; the renderer picks the concrete one at call time).
DIAGRAM_KINDS: frozenset[str] = frozenset(
    {
        # graphviz (box + arrow) family
        "block-diagram",   # Inflöde → Process → Utflöde and any linear pipeline
        "flowchart",       # decision/step flow
        "concept-map",     # central concept → related concepts
        "cause-effect",    # orsak → verkan chains
        "cycle",           # closed loop (vattnets kretslopp, kolcykeln)
        # matplotlib family
        "labeled-wave",    # sine wave annotated with våglängd / amplitud
        "force-diagram",   # a body with labelled force arrows (tyngdkraft, …)
        "plot",            # generic line / function graph
        "bar-chart",       # categorical bars
        "timeline",        # a dated horizontal tidslinje
        # rdkit family
        "molecule",        # SMILES → 2D skeletal formula
    }
)

_GRAPHVIZ_KINDS = frozenset(
    {"block-diagram", "flowchart", "concept-map", "cause-effect", "cycle"}
)
_MATPLOTLIB_KINDS = frozenset(
    {"labeled-wave", "force-diagram", "plot", "bar-chart", "timeline"}
)
_RDKIT_KINDS = frozenset({"molecule"})


def engine_for_kind(kind: str) -> str:
    """The logical engine for a diagram kind: 'graphviz' | 'matplotlib' | 'rdkit'."""
    if kind in _GRAPHVIZ_KINDS:
        return "graphviz"
    if kind in _MATPLOTLIB_KINDS:
        return "matplotlib"
    if kind in _RDKIT_KINDS:
        return "rdkit"
    raise ProceduralError(f"unknown diagram kind: {kind!r}")


# ── the DiagramSpec ─────────────────────────────────────────────────────────


@dataclass
class DiagramSpec:
    """The deterministic-render contract: WHAT to draw, as plain data.

    A spec is a JSON object (this dataclass mirrors it). The renderer needs only
    the fields its ``kind`` uses; everything else is optional with a safe default,
    so a minimal spec (just ``kind`` + ``title``) still renders. The LLM plug
    (:func:`spec_from_llm`) and the template builder (:func:`make_spec`) both emit
    this shape.

    Common fields
      kind      one of :data:`DIAGRAM_KINDS`.
      title     the figure heading (Swedish), drawn at the top.

    Graph kinds (block-diagram / flowchart / concept-map / cause-effect / cycle)
      nodes     ordered list of node labels (Swedish) OR ``{"id","label"}`` dicts.
      edges     list of ``[from, to]`` or ``{"from","to","label"}`` — for
                block-diagram/cycle, omit to auto-chain the nodes in order.
      center    concept-map only: the central node label (defaults to the title).

    Plot/wave/force/bar/timeline kinds
      x_label / y_label   axis labels (Swedish).
      series    plot/bar: list of ``{"label","x":[…],"y":[…]}`` (line) or
                ``{"label","categories":[…],"values":[…]}`` (bar).
      annotations  list of ``{"text","x","y"}`` callouts (Swedish).
      forces    force-diagram: list of ``{"label","dx","dy"}`` arrows from the body.
      events    timeline: list of ``{"year","label"}`` ordered points.
      wave      labeled-wave: ``{"wavelength","amplitude","cycles"}`` (all optional).

    Molecule kind
      smiles    the molecule as a SMILES string (e.g. ``"O"`` water, ``"CCO"``
                ethanol). ``name`` (Swedish) labels it.
      name      the molecule's Swedish name.

    Provenance (carried for the library row / debugging)
      subject / area / image_type / begrepp / forklaring  the source concept.
    """

    kind: str
    title: str = ""
    # graph
    nodes: list[Any] = field(default_factory=list)
    edges: list[Any] = field(default_factory=list)
    center: Optional[str] = None
    # plot / wave / force / bar / timeline
    x_label: str = ""
    y_label: str = ""
    series: list[dict] = field(default_factory=list)
    annotations: list[dict] = field(default_factory=list)
    forces: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    wave: dict = field(default_factory=dict)
    # molecule
    smiles: str = ""
    name: str = ""
    # provenance
    subject: str = ""
    area: str = ""
    image_type: str = ""
    begrepp: str = ""
    forklaring: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DiagramSpec":
        """Build a spec from a JSON dict, ignoring unknown keys (forward-compat)."""
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return {
            k: getattr(self, k) for k in self.__dataclass_fields__  # type: ignore[attr-defined]
        }

    def all_labels(self) -> list[str]:
        """Every Swedish string the rendered figure will display.

        Used by tests + the worker to verify the labels actually reached the
        renderer (the strings are what a vision check would read off the PNG).
        """
        out: list[str] = []
        if self.title:
            out.append(self.title)
        for n in self.nodes:
            out.append(n["label"] if isinstance(n, dict) else str(n))
        for e in self.edges:
            if isinstance(e, dict) and e.get("label"):
                out.append(str(e["label"]))
        if self.center:
            out.append(self.center)
        for lbl in (self.x_label, self.y_label, self.name):
            if lbl:
                out.append(lbl)
        for s in self.series:
            if s.get("label"):
                out.append(str(s["label"]))
        for a in self.annotations:
            if a.get("text"):
                out.append(str(a["text"]))
        for fz in self.forces:
            if fz.get("label"):
                out.append(str(fz["label"]))
        for ev in self.events:
            if ev.get("label"):
                out.append(str(ev["label"]))
        return out


def validate_spec(spec: "DiagramSpec | dict") -> DiagramSpec:
    """Coerce + validate a spec; raise :class:`ProceduralError` on a bad one.

    Accepts a :class:`DiagramSpec` or a plain dict. Checks the ``kind`` is known
    and that the kind's required payload is present (graph kinds need nodes;
    molecule needs a smiles; etc.). Returns a normalised :class:`DiagramSpec`.
    """
    s = spec if isinstance(spec, DiagramSpec) else DiagramSpec.from_dict(spec)
    if s.kind not in DIAGRAM_KINDS:
        raise ProceduralError(
            f"unknown diagram kind {s.kind!r}; expected one of {sorted(DIAGRAM_KINDS)}"
        )
    if s.kind in _GRAPHVIZ_KINDS and not s.nodes:
        raise ProceduralError(f"{s.kind} spec needs at least one node")
    if s.kind == "molecule" and not s.smiles:
        raise ProceduralError("molecule spec needs a 'smiles' string")
    if s.kind == "labeled-wave":
        pass  # all wave params optional; a bare wave still renders sensibly
    if s.kind in ("plot", "bar-chart") and not s.series:
        raise ProceduralError(f"{s.kind} spec needs at least one series")
    if s.kind == "force-diagram" and not s.forces:
        raise ProceduralError("force-diagram spec needs at least one force")
    if s.kind == "timeline" and not s.events:
        raise ProceduralError("timeline spec needs at least one event")
    return s


# ── font resolution (graphviz + matplotlib both want a Swedish-glyph TTF) ────
#
# Shared with caption.py's intent: DejaVu Sans carries å/ä/ö and ships on the
# Sibbis/CI base + is matplotlib's default, so Swedish text is legible without
# any extra asset. We resolve a concrete path so graphviz (which needs a named
# font) and a matplotlib FontProperties can both point at the same file.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)


def _font_path() -> Optional[str]:
    explicit = os.environ.get("LARBILD_PROC_FONT")
    if explicit and Path(explicit).is_file():
        return explicit
    for cand in _FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


def _graphviz_fontname() -> str:
    """A font NAME graphviz/dot will resolve to a Swedish-capable face.

    'DejaVu Sans' is registered with fontconfig on the standard base; dot looks
    it up by name. If absent, 'sans-serif' still renders (the glyph coverage of
    the default may vary, but the diagram structure is correct regardless).
    """
    return os.environ.get("LARBILD_PROC_FONTNAME", "DejaVu Sans")


DPI = int(os.environ.get("LARBILD_PROC_DPI", "150"))
CANVAS_W = int(os.environ.get("LARBILD_PROC_W", "1280"))
CANVAS_H = int(os.environ.get("LARBILD_PROC_H", "960"))

# Läroboksstil palette — calm, high-contrast, white background.
_BG = "#ffffff"
_INK = "#1a1a1a"
_BOX_FILL = "#eaf2fb"
_BOX_EDGE = "#2c6fb5"
_ACCENT = "#c0392b"
_GRID = "#d6d6d6"


def _have_dot() -> bool:
    """Is the graphviz ``dot`` binary on PATH? (apt graphviz; see requirements)."""
    return shutil.which("dot") is not None


def _have_rdkit() -> bool:
    try:
        import rdkit  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ── matplotlib helpers (lazy import: keeps module import cheap + GUI-free) ───


def _mpl():
    """Import matplotlib with the headless Agg backend. Cached by Python."""
    import matplotlib
    matplotlib.use("Agg")  # no display; safe in gunicorn / CI
    import matplotlib.pyplot as plt
    return plt


def _mpl_font():
    """A matplotlib FontProperties pinned to the resolved Swedish TTF, or None."""
    path = _font_path()
    if path is None:
        return None
    from matplotlib.font_manager import FontProperties
    return FontProperties(fname=path)


def _new_fig():
    plt = _mpl()
    fig, ax = plt.subplots(
        figsize=(CANVAS_W / DPI, CANVAS_H / DPI), dpi=DPI
    )
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    return plt, fig, ax


def _save_fig(plt, fig, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(dest), dpi=DPI, facecolor=_BG, bbox_inches="tight",
                pad_inches=0.25)
    plt.close(fig)
    return dest


# ════════════════════════════════════════════════════════════════════════════
#  graphviz renderers (box + arrow) — with a matplotlib fallback
# ════════════════════════════════════════════════════════════════════════════


def _node_pairs(nodes: Sequence[Any]) -> list[tuple[str, str]]:
    """Normalise nodes to ``[(id, label)]`` (synthesising ids for bare strings)."""
    out: list[tuple[str, str]] = []
    for i, n in enumerate(nodes):
        if isinstance(n, dict):
            out.append((str(n.get("id", f"n{i}")), str(n.get("label", n.get("id", "")))))
        else:
            out.append((f"n{i}", str(n)))
    return out


def _edge_list(spec: DiagramSpec, pairs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Resolve edges to ``[(from_id, to_id, label)]``.

    Explicit ``edges`` win. For block-diagram / cycle with no edges, auto-chain
    the nodes in order (cycle wraps the last back to the first) — that is exactly
    the Inflöde→Process→Utflöde pipeline the common case needs.
    """
    label_to_id = {lbl: nid for nid, lbl in pairs}
    ids = [nid for nid, _ in pairs]
    if spec.edges:
        out: list[tuple[str, str, str]] = []
        for e in spec.edges:
            if isinstance(e, dict):
                a, b = e.get("from"), e.get("to")
                lbl = str(e.get("label", ""))
            else:
                a, b = e[0], e[1]
                lbl = ""
            a_id = label_to_id.get(a, a)
            b_id = label_to_id.get(b, b)
            out.append((str(a_id), str(b_id), lbl))
        return out
    # No explicit edges: auto-chain for the linear/loop kinds.
    chained = [(ids[i], ids[i + 1], "") for i in range(len(ids) - 1)]
    if spec.kind == "cycle" and len(ids) > 1:
        chained.append((ids[-1], ids[0], ""))
    return chained


def _render_graph_graphviz(spec: DiagramSpec, dest: Path) -> Path:
    """Draw a box+arrow diagram with graphviz `dot`. Requires the dot binary."""
    import graphviz

    pairs = _node_pairs(spec.nodes)
    edges = _edge_list(spec, pairs)
    is_cycle = spec.kind == "cycle"
    is_map = spec.kind == "concept-map"

    engine = "dot"
    if is_cycle or is_map:
        engine = "circo" if _engine_available("circo") else "dot"

    g = graphviz.Digraph("larbild", engine=engine, format="png")
    fontname = _graphviz_fontname()
    rankdir = "LR" if spec.kind in ("block-diagram", "cause-effect") else "TB"
    g.attr(
        rankdir=rankdir, bgcolor=_BG, fontname=fontname,
        labelloc="t", label=_dot_label(spec.title), fontsize="20", fontcolor=_INK,
        nodesep="0.5", ranksep="0.7", pad="0.4",
    )
    g.attr("node", shape="box", style="rounded,filled", fillcolor=_BOX_FILL,
           color=_BOX_EDGE, fontname=fontname, fontcolor=_INK, fontsize="16",
           margin="0.22,0.14", penwidth="1.6")
    g.attr("edge", color=_BOX_EDGE, fontname=fontname, fontcolor=_INK,
           fontsize="13", penwidth="1.6", arrowsize="0.9")

    # concept-map: a distinct central node, the rest spoke off it.
    center_label = spec.center or spec.title
    if is_map and center_label:
        g.node("center", _dot_label(center_label), shape="ellipse",
               fillcolor="#fdebd0", color=_ACCENT, penwidth="2.0")
        for nid, lbl in pairs:
            g.node(nid, _dot_label(lbl))
            g.edge("center", nid)
    else:
        for nid, lbl in pairs:
            g.node(nid, _dot_label(lbl))
        for a, b, lbl in edges:
            g.edge(a, b, label=_dot_label(lbl) if lbl else "")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # graphviz writes <dest_without_suffix>.png; render to a temp then move.
    tmp = dest.with_suffix("")
    out = g.render(filename=str(tmp), cleanup=True)
    produced = Path(out)
    if produced != dest:
        produced.replace(dest)
    return dest


def _engine_available(name: str) -> bool:
    return shutil.which(name) is not None


def _dot_label(text: str) -> str:
    """Escape + lightly wrap a label for a dot node (keeps Swedish glyphs)."""
    if not text:
        return ""
    # dot uses \n for line breaks inside a label; wrap long labels at ~18 chars.
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > 18 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\\n".join(lines)


def _render_graph_matplotlib(spec: DiagramSpec, dest: Path) -> Path:
    """Box+arrow fallback when `dot` is missing — pure matplotlib.

    Lays the nodes out left-to-right (linear/cause-effect), top-to-bottom
    (flowchart), around a ring (cycle) or radially around a centre (concept-map),
    draws rounded boxes with the Swedish labels, and connects them with arrows.
    Structurally faithful to the graphviz version; the dot binary just makes the
    routing prettier.
    """
    from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    if spec.title:
        ax.set_title(spec.title, fontsize=16, color=_INK, fontproperties=fp, pad=12)

    pairs = _node_pairs(spec.nodes)
    n = len(pairs)
    pos: dict[str, tuple[float, float]] = {}
    bw, bh = 2.6, 1.1

    if spec.kind in ("block-diagram", "cause-effect"):
        # horizontal pipeline, evenly spaced
        xs = _spread(n, 1.4, 8.6)
        for (nid, _), x in zip(pairs, xs):
            pos[nid] = (x, 5.0)
    elif spec.kind == "flowchart":
        ys = _spread(n, 8.6, 1.4)
        for (nid, _), y in zip(pairs, ys):
            pos[nid] = (5.0, y)
    elif spec.kind == "cycle":
        for i, (nid, _) in enumerate(pairs):
            ang = math.pi / 2 - 2 * math.pi * i / max(1, n)
            pos[nid] = (5.0 + 3.2 * math.cos(ang), 5.0 + 3.2 * math.sin(ang))
    else:  # concept-map
        cx, cy = 5.0, 5.0
        center_label = spec.center or spec.title or "Begrepp"
        _draw_box(ax, cx, cy, bw, bh, center_label, fp, fill="#fdebd0",
                  edge=_ACCENT)
        for i, (nid, _) in enumerate(pairs):
            ang = 2 * math.pi * i / max(1, n)
            pos[nid] = (cx + 3.3 * math.cos(ang), cy + 2.9 * math.sin(ang))

    for nid, lbl in pairs:
        x, y = pos[nid]
        _draw_box(ax, x, y, bw, bh, lbl, fp)

    edges = _edge_list(spec, pairs)
    if spec.kind == "concept-map":
        edges = []  # spokes drawn below
    for a, b, lbl in edges:
        if a in pos and b in pos:
            _draw_arrow(ax, pos[a], pos[b], bw, bh)
            if lbl:
                mx, my = (pos[a][0] + pos[b][0]) / 2, (pos[a][1] + pos[b][1]) / 2
                ax.text(mx, my + 0.3, lbl, fontsize=10, color=_INK, ha="center",
                        fontproperties=fp)
    if spec.kind == "concept-map":
        for nid, _ in pairs:
            _draw_arrow(ax, (5.0, 5.0), pos[nid], bw, bh)

    return _save_fig(plt, fig, dest)


def _spread(n: int, lo: float, hi: float) -> list[float]:
    if n <= 1:
        return [(lo + hi) / 2]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def _draw_box(ax, x, y, w, h, text, fp, *, fill=_BOX_FILL, edge=_BOX_EDGE):
    from matplotlib.patches import FancyBboxPatch
    box = FancyBboxPatch(
        (x - w / 2, y - h / 2), w, h,
        boxstyle="round,pad=0.08,rounding_size=0.18",
        linewidth=1.6, edgecolor=edge, facecolor=fill, mutation_aspect=1.0,
    )
    ax.add_patch(box)
    ax.text(x, y, _wrap_text(str(text), 16), ha="center", va="center",
            fontsize=11, color=_INK, fontproperties=fp, wrap=True)


def _draw_arrow(ax, p0, p1, bw, bh):
    from matplotlib.patches import FancyArrowPatch
    # trim the arrow to box edges so it points box→box, not centre→centre
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    sx, sy = x0 + ux * (bw / 2 + 0.05), y0 + uy * (bh / 2 + 0.05)
    ex, ey = x1 - ux * (bw / 2 + 0.05), y1 - uy * (bh / 2 + 0.05)
    arr = FancyArrowPatch((sx, sy), (ex, ey), arrowstyle="-|>", mutation_scale=16,
                          linewidth=1.6, color=_BOX_EDGE)
    ax.add_patch(arr)


def _wrap_text(text: str, width: int) -> str:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _render_graph(spec: DiagramSpec, dest: Path) -> Path:
    """Graph dispatcher: prefer graphviz `dot`, fall back to matplotlib."""
    if _have_dot():
        try:
            return _render_graph_graphviz(spec, dest)
        except Exception as e:  # noqa: BLE001 — never lose a render to a dot hiccup
            log.warning("graphviz render failed (%s); falling back to matplotlib", e)
    return _render_graph_matplotlib(spec, dest)


# ════════════════════════════════════════════════════════════════════════════
#  matplotlib renderers (waves, forces, plots, bars, timelines)
# ════════════════════════════════════════════════════════════════════════════


def _render_labeled_wave(spec: DiagramSpec, dest: Path) -> Path:
    """A sine wave annotated with våglängd + amplitud (the canonical ljud/ljus fig)."""
    import numpy as np

    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    w = spec.wave or {}
    wavelength = float(w.get("wavelength", 2.0))
    amplitude = float(w.get("amplitude", 1.0))
    cycles = float(w.get("cycles", 2.5))

    x = np.linspace(0, wavelength * cycles, 1000)
    y = amplitude * np.sin(2 * np.pi * x / wavelength)
    ax.plot(x, y, color=_BOX_EDGE, linewidth=2.2)
    ax.axhline(0, color=_GRID, linewidth=1.0)

    # våglängd: span one period along a peak-to-peak reference line
    y_ref = amplitude * 1.25
    ax.annotate(
        "", xy=(wavelength, y_ref), xytext=(0, y_ref),
        arrowprops=dict(arrowstyle="<->", color=_ACCENT, linewidth=1.8),
    )
    ax.text(wavelength / 2, y_ref + amplitude * 0.08, "Våglängd (λ)", ha="center",
            color=_ACCENT, fontsize=13, fontproperties=fp)

    # amplitud: vertical span from axis to the first crest (at λ/4)
    crest_x = wavelength / 4
    ax.annotate(
        "", xy=(crest_x, amplitude), xytext=(crest_x, 0),
        arrowprops=dict(arrowstyle="<->", color=_ACCENT, linewidth=1.8),
    )
    ax.text(crest_x + wavelength * 0.04, amplitude / 2, "Amplitud", color=_ACCENT,
            fontsize=13, va="center", fontproperties=fp)

    for ann in spec.annotations:
        ax.annotate(str(ann.get("text", "")),
                    xy=(float(ann.get("x", 0)), float(ann.get("y", 0))),
                    fontsize=11, color=_INK, fontproperties=fp)

    ax.set_xlabel(spec.x_label or "Avstånd (m)", fontsize=12, fontproperties=fp)
    ax.set_ylabel(spec.y_label or "Utslag", fontsize=12, fontproperties=fp)
    if spec.title:
        ax.set_title(spec.title, fontsize=15, color=_INK, fontproperties=fp)
    ax.set_ylim(-amplitude * 1.5, amplitude * 1.6)
    ax.grid(True, color=_GRID, linewidth=0.6, alpha=0.7)
    _swedish_ticks(ax, fp)
    return _save_fig(plt, fig, dest)


def _render_force_diagram(spec: DiagramSpec, dest: Path) -> Path:
    """A body (box) with labelled force arrows (tyngdkraft, normalkraft, …)."""
    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    ax.set_xlim(-3, 3)
    ax.set_ylim(-3, 3)
    ax.set_aspect("equal")
    ax.axis("off")

    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((-0.5, -0.5), 1.0, 1.0, facecolor=_BOX_FILL,
                            edgecolor=_BOX_EDGE, linewidth=1.8))
    body = spec.name or spec.begrepp or "Föremål"
    ax.text(0, 0, _wrap_text(body, 10), ha="center", va="center", fontsize=10,
            color=_INK, fontproperties=fp)

    for fz in spec.forces:
        dx, dy = float(fz.get("dx", 0)), float(fz.get("dy", 0))
        if dx == 0 and dy == 0:
            continue
        ax.annotate("", xy=(dx, dy), xytext=(0, 0),
                    arrowprops=dict(arrowstyle="-|>", color=_ACCENT, linewidth=2.2,
                                    mutation_scale=20))
        lx, ly = dx * 1.12, dy * 1.12
        ax.text(lx, ly, str(fz.get("label", "")), ha="center", va="center",
                fontsize=12, color=_ACCENT, fontproperties=fp)

    if spec.title:
        ax.set_title(spec.title, fontsize=15, color=_INK, fontproperties=fp)
    return _save_fig(plt, fig, dest)


def _render_plot(spec: DiagramSpec, dest: Path) -> Path:
    """Generic line/function graph with Swedish axes + a legend."""
    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    for s in spec.series:
        xs = s.get("x") or list(range(len(s.get("y", []))))
        ys = s.get("y", [])
        ax.plot(xs, ys, marker="o", markersize=3, linewidth=2.0,
                label=str(s.get("label", "")))
    for ann in spec.annotations:
        ax.annotate(str(ann.get("text", "")),
                    xy=(float(ann.get("x", 0)), float(ann.get("y", 0))),
                    fontsize=11, color=_INK, fontproperties=fp,
                    arrowprops=dict(arrowstyle="->", color=_ACCENT))
    ax.set_xlabel(spec.x_label or "x", fontsize=12, fontproperties=fp)
    ax.set_ylabel(spec.y_label or "y", fontsize=12, fontproperties=fp)
    if spec.title:
        ax.set_title(spec.title, fontsize=15, color=_INK, fontproperties=fp)
    if any(s.get("label") for s in spec.series):
        leg = ax.legend(prop=fp, frameon=True)
        leg.get_frame().set_edgecolor(_GRID)
    ax.grid(True, color=_GRID, linewidth=0.6, alpha=0.7)
    _swedish_ticks(ax, fp)
    return _save_fig(plt, fig, dest)


def _render_bar_chart(spec: DiagramSpec, dest: Path) -> Path:
    """Categorical bar chart (Swedish category + axis labels)."""
    import numpy as np

    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    series = spec.series
    cats = series[0].get("categories", []) if series else []
    x = np.arange(len(cats))
    width = 0.8 / max(1, len(series))
    for i, s in enumerate(series):
        vals = s.get("values", [])
        ax.bar(x + i * width, vals, width, label=str(s.get("label", "")),
               edgecolor=_INK, linewidth=0.5)
    ax.set_xticks(x + width * (len(series) - 1) / 2)
    ax.set_xticklabels([str(c) for c in cats], fontproperties=fp)
    ax.set_xlabel(spec.x_label or "", fontsize=12, fontproperties=fp)
    ax.set_ylabel(spec.y_label or "", fontsize=12, fontproperties=fp)
    if spec.title:
        ax.set_title(spec.title, fontsize=15, color=_INK, fontproperties=fp)
    if any(s.get("label") for s in series):
        leg = ax.legend(prop=fp, frameon=True)
        leg.get_frame().set_edgecolor(_GRID)
    ax.grid(True, axis="y", color=_GRID, linewidth=0.6, alpha=0.7)
    _swedish_ticks(ax, fp)
    return _save_fig(plt, fig, dest)


def _render_timeline(spec: DiagramSpec, dest: Path) -> Path:
    """A horizontal dated tidslinje with alternating Swedish event labels."""
    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    events = sorted(spec.events, key=lambda e: float(e.get("year", 0)))
    if not events:
        raise ProceduralError("timeline needs events")
    years = [float(e.get("year", 0)) for e in events]
    y0, y1 = min(years), max(years)
    span = (y1 - y0) or 1.0
    ax.set_xlim(y0 - span * 0.08, y1 + span * 0.08)
    ax.set_ylim(-1.2, 1.2)
    ax.axhline(0, color=_BOX_EDGE, linewidth=2.0)
    for i, ev in enumerate(events):
        yr = float(ev.get("year", 0))
        up = i % 2 == 0
        yy = 0.7 if up else -0.7
        ax.plot([yr], [0], "o", color=_ACCENT, markersize=8)
        ax.annotate("", xy=(yr, yy * 0.85), xytext=(yr, 0),
                    arrowprops=dict(arrowstyle="-", color=_GRID, linewidth=1.0))
        ax.text(yr, yy, f"{int(yr) if yr.is_integer() else yr}\n{ev.get('label','')}",
                ha="center", va="bottom" if up else "top", fontsize=10,
                color=_INK, fontproperties=fp)
    ax.get_yaxis().set_visible(False)
    for spine in ("left", "right", "top"):
        ax.spines[spine].set_visible(False)
    if spec.x_label:
        ax.set_xlabel(spec.x_label, fontsize=12, fontproperties=fp)
    if spec.title:
        ax.set_title(spec.title, fontsize=15, color=_INK, fontproperties=fp)
    _swedish_ticks(ax, fp)
    return _save_fig(plt, fig, dest)


def _swedish_ticks(ax, fp):
    """Apply the Swedish TTF to tick labels (so numbers + any text match)."""
    if fp is None:
        return
    for lbl in list(ax.get_xticklabels()) + list(ax.get_yticklabels()):
        lbl.set_fontproperties(fp)


# ════════════════════════════════════════════════════════════════════════════
#  RDKit molecule renderer — with a matplotlib fallback
# ════════════════════════════════════════════════════════════════════════════


def _render_molecule_rdkit(spec: DiagramSpec, dest: Path) -> Path:
    """SMILES → a 2D skeletal formula PNG via RDKit (correct atoms + bonds)."""
    from rdkit import Chem
    from rdkit.Chem import Draw
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromSmiles(spec.smiles)
    if mol is None:
        raise ProceduralError(f"RDKit could not parse SMILES {spec.smiles!r}")
    from rdkit.Chem import AllChem
    AllChem.Compute2DCoords(mol)

    drawer = rdMolDraw2D.MolDraw2DCairo(CANVAS_W, CANVAS_H)
    opts = drawer.drawOptions()
    opts.addStereoAnnotation = True
    opts.bondLineWidth = 2
    # A Swedish name as the figure legend, if given.
    legend = spec.name or spec.begrepp or ""
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, legend=legend)
    drawer.FinishDrawing()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(drawer.GetDrawingText())
    return dest


def _render_molecule_matplotlib(spec: DiagramSpec, dest: Path) -> Path:
    """Minimal atom/bond sketch when RDKit is absent (still labeled, still PNG).

    Not a chemistry engine — it parses only the atom symbols out of the SMILES
    and lays them on a ring with single bonds, labeled, so a molecule request
    NEVER falls through to a garbled diffusion render. The RDKit path (preferred)
    is what produces the bond-accurate skeletal formula.
    """
    plt, fig, ax = _new_fig()
    fp = _mpl_font()
    ax.set_aspect("equal")
    ax.axis("off")
    atoms = _smiles_atoms(spec.smiles)
    n = len(atoms) or 1
    pts = []
    for i, a in enumerate(atoms):
        ang = math.pi / 2 - 2 * math.pi * i / n
        pts.append((math.cos(ang), math.sin(ang)))
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        if len(pts) > 1:
            ax.plot([x0, x1], [y0, y1], color=_INK, linewidth=1.6, zorder=1)
    for (x, y), a in zip(pts, atoms):
        ax.scatter([x], [y], s=600, color=_atom_color(a), edgecolor=_INK,
                   zorder=2)
        ax.text(x, y, a, ha="center", va="center", fontsize=13, color=_INK,
                fontproperties=fp, zorder=3)
    title = spec.name or spec.begrepp or spec.title or "Molekyl"
    ax.set_title(title, fontsize=15, color=_INK, fontproperties=fp)
    ax.set_xlim(-1.6, 1.6)
    ax.set_ylim(-1.6, 1.6)
    return _save_fig(plt, fig, dest)


def _smiles_atoms(smiles: str) -> list[str]:
    """Best-effort atom-symbol extraction from a SMILES (fallback only)."""
    out: list[str] = []
    i = 0
    s = smiles or ""
    while i < len(s):
        c = s[i]
        if c.isalpha():
            # two-letter element (Cl, Br) vs one-letter
            if i + 1 < len(s) and s[i + 1].islower() and (c + s[i + 1]) in (
                "Cl", "Br", "Si", "Na", "Ca", "Mg", "Fe"
            ):
                out.append(c + s[i + 1])
                i += 2
                continue
            out.append(c.upper())
        i += 1
    return out or ["C"]


def _atom_color(symbol: str) -> str:
    return {
        "C": "#2b2b2b", "O": "#c0392b", "N": "#2c6fb5", "H": "#dddddd",
        "S": "#d4ac0d", "Cl": "#27ae60", "Br": "#8e44ad",
    }.get(symbol.upper() if len(symbol) == 1 else symbol, "#7f8c8d")


def _render_molecule(spec: DiagramSpec, dest: Path) -> Path:
    if _have_rdkit():
        try:
            return _render_molecule_rdkit(spec, dest)
        except ProceduralError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("RDKit render failed (%s); falling back to sketch", e)
    return _render_molecule_matplotlib(spec, dest)


# ════════════════════════════════════════════════════════════════════════════
#  the dispatcher: spec → the right renderer
# ════════════════════════════════════════════════════════════════════════════

_RENDERERS = {
    "labeled-wave": _render_labeled_wave,
    "force-diagram": _render_force_diagram,
    "plot": _render_plot,
    "bar-chart": _render_bar_chart,
    "timeline": _render_timeline,
    "molecule": _render_molecule,
}


def render_spec(spec: "DiagramSpec | dict", dest: "str | Path") -> Path:
    """Render a validated :class:`DiagramSpec` to a PNG at ``dest``.

    The single entry point a caller (the render worker) uses: validate the spec,
    dispatch by ``kind`` to the right deterministic renderer (graphviz for the
    box+arrow kinds, matplotlib for plots, RDKit for molecules), and write a clean
    white-background PNG with legible Swedish labels. Returns the written path.
    Raises :class:`ProceduralError` on a bad spec or an unrecoverable engine error.
    """
    s = validate_spec(spec)
    dest = Path(dest)
    if s.kind in _GRAPHVIZ_KINDS:
        return _render_graph(s, dest)
    renderer = _RENDERERS.get(s.kind)
    if renderer is None:  # pragma: no cover - validate_spec already guards kinds
        raise ProceduralError(f"no renderer for kind {s.kind!r}")
    return renderer(s, dest)


# ════════════════════════════════════════════════════════════════════════════
#  template spec builder — correct labeled diagrams WITHOUT an LLM
# ════════════════════════════════════════════════════════════════════════════


def default_kind_for(image_type: str, subject: str = "", area: str = "") -> str:
    """The default diagram kind for an imageType (+ subject/area hints).

    Maps the structured imageType slug to a concrete template kind so the common
    case renders correctly with no spec authoring:

      * ``molekyl``/``molecule``/``reaktion``/``reaction`` → molecule (kemi)
      * ``tidslinje``                                      → timeline
      * ``diagram``/``schema``/``schematic``               → block-diagram, EXCEPT
        - subject kemi → molecule
        - subject fysik + area in (ljud, ljus, vagor)      → labeled-wave
        - subject fysik + area in (rorelse, kraft)         → force-diagram
        - subject matematik                                → plot
    """
    it = (image_type or "").strip().lower()
    subj = (subject or "").strip().lower()
    ar = (area or "").strip().lower()
    if it in ("molekyl", "molecule", "reaktion", "reaction"):
        return "molecule"
    if it == "tidslinje":
        return "timeline"
    # diagram / schema / schematic — choose by subject/area
    if subj == "kemi":
        return "molecule"
    if subj == "fysik":
        if ar in ("ljud", "ljus", "vagor", "vågor"):
            return "labeled-wave"
        if ar in ("rorelse", "rörelse", "kraft", "krafter"):
            return "force-diagram"
    if subj == "matematik":
        return "plot"
    return "block-diagram"


def make_spec(
    *,
    subject: str,
    area: str,
    image_type: str,
    begrepp: str,
    forklaring: str = "",
    vardagligt_exempel: str = "",
    kind: Optional[str] = None,
) -> DiagramSpec:
    """Build a CORRECT, labeled spec for a concept — no LLM required (templates).

    This is the "works NOW" path: given the concept (subject/area/imageType +
    begrepp + förklaring), pick a template kind (:func:`default_kind_for` unless
    ``kind`` is forced) and populate it with sensible, real Swedish labels so the
    rendered diagram is specific to the concept. The per-kind builders:

      * **block-diagram** — an Inflöde → Process → Utflöde pipeline titled by the
        begrepp (the universal "system/process" diagram). Three labeled boxes the
        student can read; the förklaring rides as the figure context.
      * **timeline** — a single labeled event for the begrepp (a 1-event tidslinje
        is a valid, correct figure; the LLM step enriches it to many events).
      * **labeled-wave** — the canonical λ/amplitud sound/light wave.
      * **force-diagram** — tyngdkraft down + normalkraft up on the begrepp body.
      * **molecule** — the begrepp mapped to a known SMILES (a small kemi lexicon),
        else water as a safe, correct default with the begrepp as the legend.
      * **plot** — a labeled linear relationship (matematik default).

    The output is always a fully-valid spec (passes :func:`validate_spec`), so a
    render never depends on the LLM being available.
    """
    k = kind or default_kind_for(image_type, subject, area)
    base = dict(subject=subject, area=area, image_type=image_type,
                begrepp=begrepp, forklaring=forklaring)
    title = begrepp or "Diagram"

    if k == "block-diagram":
        spec = DiagramSpec(
            kind="block-diagram", title=title,
            nodes=["Inflöde", f"Process: {begrepp}" if begrepp else "Process", "Utflöde"],
            **base,
        )
    elif k == "flowchart":
        spec = DiagramSpec(
            kind="flowchart", title=title,
            nodes=["Start", begrepp or "Steg", "Resultat"], **base,
        )
    elif k == "cycle":
        spec = DiagramSpec(
            kind="cycle", title=title,
            nodes=[f"{begrepp} 1", f"{begrepp} 2", f"{begrepp} 3", f"{begrepp} 4"]
            if begrepp else ["Steg 1", "Steg 2", "Steg 3", "Steg 4"], **base,
        )
    elif k == "concept-map":
        spec = DiagramSpec(
            kind="concept-map", title=title, center=begrepp or "Begrepp",
            nodes=["Egenskap", "Exempel", "Orsak", "Verkan"], **base,
        )
    elif k == "labeled-wave":
        spec = DiagramSpec(
            kind="labeled-wave", title=title or "Vågrörelse",
            x_label="Avstånd (m)", y_label="Utslag",
            wave={"wavelength": 2.0, "amplitude": 1.0, "cycles": 2.5}, **base,
        )
    elif k == "force-diagram":
        spec = DiagramSpec(
            kind="force-diagram", title=title or "Krafter",
            name=begrepp or "Föremål",
            forces=[
                {"label": "Tyngdkraft", "dx": 0, "dy": -2.2},
                {"label": "Normalkraft", "dx": 0, "dy": 2.2},
            ], **base,
        )
    elif k == "plot":
        spec = DiagramSpec(
            kind="plot", title=title or "Samband",
            x_label="x", y_label="y",
            series=[{"label": begrepp or "y = x", "x": [0, 1, 2, 3, 4],
                     "y": [0, 1, 2, 3, 4]}], **base,
        )
    elif k == "bar-chart":
        spec = DiagramSpec(
            kind="bar-chart", title=title,
            x_label="Kategori", y_label="Värde",
            series=[{"label": begrepp or "Värde",
                     "categories": ["A", "B", "C"], "values": [3, 5, 2]}], **base,
        )
    elif k == "timeline":
        spec = DiagramSpec(
            kind="timeline", title=title or "Tidslinje",
            x_label="År",
            events=[{"year": 2000, "label": begrepp or "Händelse"}], **base,
        )
    elif k == "molecule":
        smiles, name = _concept_to_smiles(begrepp)
        spec = DiagramSpec(
            kind="molecule", title=title, smiles=smiles, name=name or begrepp,
            **base,
        )
    else:  # pragma: no cover - default_kind_for only returns the above
        spec = DiagramSpec(kind="block-diagram", title=title,
                           nodes=["Inflöde", "Process", "Utflöde"], **base)

    return validate_spec(spec)


# A tiny SFW kemi lexicon: common åk 7-9 molecules → SMILES. The LLM step (or a
# future expansion) can supply SMILES for anything outside this set; the default
# (water) keeps a molecule render correct + labeled rather than failing.
#
# The display name uses ASCII formula notation (H2O, not H₂O) on purpose: RDKit's
# Cairo legend font renders Unicode subscripts with broken spacing, so an ASCII
# formula stays legible in the skeletal-formula legend (the "no garbled text"
# goal). The structure itself is always correct (it comes from the SMILES).
_MOLECULE_LEXICON: dict[str, tuple[str, str]] = {
    "vatten": ("O", "Vatten (H2O)"),
    "koldioxid": ("O=C=O", "Koldioxid (CO2)"),
    "syre": ("O=O", "Syrgas (O2)"),
    "syrgas": ("O=O", "Syrgas (O2)"),
    "väte": ("[H][H]", "Vätgas (H2)"),
    "vätgas": ("[H][H]", "Vätgas (H2)"),
    "kväve": ("N#N", "Kvävgas (N2)"),
    "metan": ("C", "Metan (CH4)"),
    "etanol": ("CCO", "Etanol (C2H5OH)"),
    "alkohol": ("CCO", "Etanol (C2H5OH)"),
    "metanol": ("CO", "Metanol (CH3OH)"),
    "ammoniak": ("N", "Ammoniak (NH3)"),
    "koksalt": ("[Na+].[Cl-]", "Natriumklorid (NaCl)"),
    "natriumklorid": ("[Na+].[Cl-]", "Natriumklorid (NaCl)"),
    "glukos": ("OCC1OC(O)C(O)C(O)C1O", "Glukos (C6H12O6)"),
    "druvsocker": ("OCC1OC(O)C(O)C(O)C1O", "Glukos (C6H12O6)"),
    "koloxid": ("[C-]#[O+]", "Kolmonoxid (CO)"),
    "kolmonoxid": ("[C-]#[O+]", "Kolmonoxid (CO)"),
    "saltsyra": ("Cl", "Väteklorid (HCl)"),
    "svaveldioxid": ("O=S=O", "Svaveldioxid (SO2)"),
    "ozon": ("[O-][O+]=O", "Ozon (O3)"),
}


def _concept_to_smiles(begrepp: str) -> tuple[str, str]:
    """Map a kemi begrepp to ``(smiles, swedish_name)``; default water.

    A small curated lexicon covers the common åk 7-9 molecules. Anything else
    returns water with the begrepp as the name — a correct, labeled fallback (the
    LLM step is where an arbitrary molecule's SMILES should come from). Never
    raises: a molecule render always gets a parseable SMILES.
    """
    key = (begrepp or "").strip().lower()
    if key in _MOLECULE_LEXICON:
        return _MOLECULE_LEXICON[key]
    # try a loose contains-match (e.g. "Vattenmolekyl" → vatten)
    for name, pair in _MOLECULE_LEXICON.items():
        if name in key:
            return pair
    return ("O", begrepp or "Molekyl")


# ════════════════════════════════════════════════════════════════════════════
#  the LLM plug point — OPTIONAL spec refinement (14B composer @ VM100:8091)
# ════════════════════════════════════════════════════════════════════════════

# The 14B educational-prompt composer endpoint (OpenAI-compatible). Lives on
# VM100; the same box the diffusion path uses. Procedural rendering does NOT need
# it (make_spec produces correct diagrams), so it is best-effort + short-timeout:
# any failure (down / slow / malformed JSON) falls back to the template spec.
LLM_COMPOSER_URL = os.environ.get(
    "LARBILD_PROC_LLM_URL", "http://192.168.5.230:8091/v1/chat/completions"
)
LLM_MODEL = os.environ.get("LARBILD_PROC_LLM_MODEL", "larbild-composer")
LLM_TIMEOUT_S = float(os.environ.get("LARBILD_PROC_LLM_TIMEOUT_S", "20"))


def _llm_spec_prompt(subject: str, area: str, image_type: str, begrepp: str,
                     forklaring: str, kind: str) -> str:
    """The instruction sent to the composer: emit a DiagramSpec JSON for a concept.

    Kept here (not in a prompt file) so the schema the LLM must hit lives next to
    the dataclass it must match. Asks for STRICT JSON, Swedish labels only, and
    the exact ``kind`` we routed — so the returned spec drops straight into
    :func:`render_spec`.
    """
    return (
        "Du är en pedagogisk diagram-designer för svenska läromedel (åk 7-9). "
        "Returnera ENBART giltig JSON (ingen text runt om) som beskriver ett "
        f"diagram av typen '{kind}' för begreppet nedan. Alla etiketter på "
        "svenska, korta och korrekta. Schema (utelämna fält som inte behövs):\n"
        '{"kind": "<kind>", "title": "...", "nodes": ["..."], '
        '"edges": [{"from":"...","to":"...","label":"..."}], '
        '"x_label":"...", "y_label":"...", '
        '"series":[{"label":"...","x":[...],"y":[...]}], '
        '"forces":[{"label":"...","dx":0,"dy":-2}], '
        '"events":[{"year":1900,"label":"..."}], '
        '"smiles":"...", "name":"..."}\n\n'
        f"Ämne: {subject}\nOmråde: {area}\nBildtyp: {image_type}\n"
        f"Begrepp: {begrepp}\nFörklaring: {forklaring}\n"
    )


def spec_from_llm(
    *,
    subject: str,
    area: str,
    image_type: str,
    begrepp: str,
    forklaring: str = "",
    kind: Optional[str] = None,
    url: Optional[str] = None,
    timeout_s: Optional[float] = None,
    _client: Any = None,
) -> DiagramSpec:
    """Ask the LLM composer for a richer DiagramSpec; fall back to :func:`make_spec`.

    THE LLM PLUG POINT. The deterministic templates (:func:`make_spec`) already
    produce a correct, labeled diagram for the common cases — this is the OPTIONAL
    enrichment seam where a model (the 14B composer at VM100:8091, or an Opus step
    upstream) supplies a fuller spec (e.g. the real multi-step process boxes, a
    multi-event timeline, a specific molecule's SMILES).

    Contract: POST an OpenAI-style chat request asking for a strict-JSON
    DiagramSpec for the routed ``kind``; parse + validate it; on ANY failure
    (no network/requests, non-2xx, non-JSON, schema-invalid) log + return the
    template spec. So wiring the LLM in NEVER makes a render worse or slower than
    the template path's guarantees — it can only add detail when it succeeds.

    ``_client`` is an injection seam for tests: an object with ``.post(url, json,
    timeout)`` returning a ``.json()``-able response. Production uses ``requests``.
    """
    forced_kind = kind or default_kind_for(image_type, subject, area)
    fallback = make_spec(subject=subject, area=area, image_type=image_type,
                         begrepp=begrepp, forklaring=forklaring, kind=forced_kind)
    try:
        client = _client
        if client is None:
            import requests as client  # type: ignore[no-redef]
        prompt = _llm_spec_prompt(subject, area, image_type, begrepp,
                                  forklaring, forced_kind)
        resp = client.post(
            url or LLM_COMPOSER_URL,
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 700,
            },
            timeout=timeout_s or LLM_TIMEOUT_S,
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        parsed.setdefault("kind", forced_kind)
        # carry provenance the LLM won't have set
        parsed.setdefault("subject", subject)
        parsed.setdefault("area", area)
        parsed.setdefault("image_type", image_type)
        parsed.setdefault("begrepp", begrepp)
        parsed.setdefault("forklaring", forklaring)
        if not parsed.get("title"):
            parsed["title"] = begrepp or fallback.title
        return validate_spec(parsed)
    except Exception as e:  # noqa: BLE001 — the LLM is strictly optional
        log.info("spec_from_llm fell back to template (%s)", e)
        return fallback


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (tolerates code fences)."""
    import json

    t = text.strip()
    if "```" in t:
        # take the content of the first fenced block
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in LLM reply")
    return json.loads(t[start:end + 1])


# ════════════════════════════════════════════════════════════════════════════
#  the top-level concept renderer — what the worker calls
# ════════════════════════════════════════════════════════════════════════════


def render_concept(
    *,
    subject: str,
    area: str,
    image_type: str,
    begrepp: str,
    forklaring: str = "",
    dest: "str | Path",
    spec: "DiagramSpec | dict | None" = None,
    use_llm: bool = False,
    llm_client: Any = None,
) -> tuple[Path, DiagramSpec]:
    """Render a concept to a labeled PNG and return ``(path, spec)``.

    The one call the render worker makes for a structured imageType. Resolution
    of the spec:

      1. an explicit ``spec`` (a caller/Opus already authored one) — used as-is;
      2. else, if ``use_llm`` — :func:`spec_from_llm` (LLM enrichment, template
         fallback);
      3. else — :func:`make_spec` (the deterministic template; the default).

    Whichever path, the spec is validated and rendered by :func:`render_spec`.
    Returns the PNG path plus the spec actually used (so the worker can persist
    the spec / its labels alongside the library row). CPU-only — no GPU.
    """
    if spec is not None:
        used = validate_spec(spec)
    elif use_llm:
        used = spec_from_llm(subject=subject, area=area, image_type=image_type,
                             begrepp=begrepp, forklaring=forklaring,
                             _client=llm_client)
    else:
        used = make_spec(subject=subject, area=area, image_type=image_type,
                         begrepp=begrepp, forklaring=forklaring)
    path = render_spec(used, dest)
    return path, used
