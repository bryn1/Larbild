"""Tests for the SFW FLUX txt2img workflow assembly + FLUX LoRA allowlist
(lb FLUX lane).

The FLUX lane is the twin of the SDXL lane (test_workflow.py) for the FLUX
dev-fp8 model: a ComfyUI graph using CheckpointLoaderSimple(flux1-dev-fp8) for
the model + DualCLIPLoader(t5xxl_fp16, clip_l, type=flux) for CLIP +
VAELoader(ae.safetensors), FluxGuidance, an EmptySD3LatentImage (FLUX/SD3
16-channel latent), cfg≈1, and an OPTIONAL FLUX LoRA resolved + allowlist-checked
backend-side. The SFW injection guard applies here too, on a SEPARATE FLUX
allowlist (the two lanes' adapters are not interchangeable).
"""
from __future__ import annotations

import pytest

from app import taxonomy
from app import workflow as wf
from app.comfyui import extract_output_images


# ── the FLUX allowlist = taxonomy.FLUX_LORA names (SFW educational only) ────


def test_flux_allowlist_is_flux_lora_names():
    al = wf.flux_allowlist()
    assert al == frozenset(c["name"] for c in taxonomy.FLUX_LORA.values())
    # the two Lärbild FLUX adapters, no more.
    assert al == {"larbild/photomicrography-flux", "larbild/schematic-machinery-flux"}


def test_flux_allowlist_carries_no_nsfw_names():
    # The FLUX lane must NEVER list imagegen's NSFW FLUX adapters that also live
    # on the box (loras/bondage/*, loras/faces/*, …). The allowlist is SFW-only.
    for name in wf.flux_allowlist():
        low = name.lower()
        for tok in ("bondage", "nsfw", "face", "lustify", "wai", "pony"):
            assert tok not in low, f"NSFW-ish token {tok!r} in FLUX allowlist name {name!r}"


def test_sdxl_allowlist_unchanged_by_flux_addition():
    # Adding the FLUX lane must not perturb the SDXL allowlist (= base ∪ matrix).
    base = {b["name"] for b in taxonomy.BASE_LORA.values()}
    mat = {c["name"] for c in taxonomy.LORA_MATRIX.values()}
    assert wf.allowlist() == frozenset(base | mat)
    # And the two allowlists are disjoint (no name is on both lanes).
    assert wf.allowlist().isdisjoint(wf.flux_allowlist())


# ── backend-authoritative FLUX LoRA resolution (optional + validated) ───────


def test_resolve_flux_lora_routes_mikroskop_to_photomicrography():
    lora = wf.resolve_and_validate_flux_lora("biologi", "mikroskop")
    assert lora is not None
    assert lora["name"] == "larbild/photomicrography-flux"
    assert lora["file"].endswith(".safetensors")
    assert lora["name"] in wf.flux_allowlist()


def test_resolve_flux_lora_routes_tvarsnitt_and_diagram_to_schematic():
    for it in ("tvarsnitt", "diagram"):
        lora = wf.resolve_and_validate_flux_lora("teknik", it)
        assert lora is not None and lora["name"] == "larbild/schematic-machinery-flux"


def test_resolve_flux_lora_is_none_for_unmapped_types():
    # Most types render LoRA-free on base FLUX — None, NOT a fallback to SDXL's.
    for it in ("foto", "illustration", "karta", "tidslinje", "tecknad"):
        assert wf.resolve_and_validate_flux_lora("fysik", it) is None


def test_resolve_flux_lora_validates_slugs():
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_flux_lora("not-a-subject", "mikroskop")
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_flux_lora("fysik", "not-a-type")
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_flux_lora("", "mikroskop")


# ── the SFW injection guard on the FLUX lane ────────────────────────────────


def test_flux_offlist_lora_dict_rejected_at_assembly():
    # A crafted FLUX LoRA naming an off-allowlist (e.g. NSFW) adapter is refused
    # before any graph is produced — the FLUX injection guard.
    bad = {"name": "loras/bondage/evil-flux", "strength": 1.0}
    with pytest.raises(wf.LoraNotAllowedError) as ei:
        wf.assemble_flux_txt2img(
            subject="biologi", image_type="mikroskop",
            prompt="a single cell", seed=1, lora=bad,
        )
    assert ei.value.lora_name == "loras/bondage/evil-flux"


def test_flux_sdxl_lora_name_not_accepted_on_flux_lane():
    # An SDXL allowlist name is NOT a valid FLUX LoRA (separate adapters): a
    # crafted dict naming a real SDXL LoRA must still be rejected on the FLUX lane.
    sdxl_name = next(iter(wf.allowlist()))
    with pytest.raises(wf.LoraNotAllowedError):
        wf.assemble_flux_txt2img(
            subject="fysik", image_type="illustration",
            prompt="x", seed=1, lora={"name": sdxl_name, "strength": 0.8},
        )


# ── the assembled FLUX graph is valid + submittable ─────────────────────────


def test_flux_graph_with_lora_is_complete():
    g, m = wf.assemble_flux_txt2img(
        subject="biologi", image_type="mikroskop",
        prompt="a single animal cell under a microscope", seed=42,
    )
    # the validated FLUX node set (with a LoRA)
    assert set(g) >= {"ckpt", "clip", "fluxvae", "lora", "pos", "neg",
                      "guidance", "latent", "ksampler", "vae", "save"}
    assert g["ckpt"]["class_type"] == "CheckpointLoaderSimple"
    assert g["clip"]["class_type"] == "DualCLIPLoader"
    assert g["clip"]["inputs"]["type"] == "flux"
    assert g["fluxvae"]["class_type"] == "VAELoader"
    assert g["latent"]["class_type"] == "EmptySD3LatentImage"  # NOT EmptyLatentImage
    assert g["guidance"]["class_type"] == "FluxGuidance"
    # with a LoRA, model+clip flow through it (standard placement)
    assert g["ksampler"]["inputs"]["model"] == ["lora", 0]
    assert g["pos"]["inputs"]["clip"] == ["lora", 1]
    assert g["lora"]["inputs"]["lora_name"] == "larbild/photomicrography-flux.safetensors"
    assert m["model"] == "flux"
    assert m["lora"]["name"] == "larbild/photomicrography-flux"


def test_flux_graph_lora_free_wires_loaders_directly():
    # An unmapped type renders LoRA-free: no LoraLoader node, and model/clip wire
    # straight from the checkpoint / DualCLIPLoader.
    g, m = wf.assemble_flux_txt2img(
        subject="fysik", image_type="illustration",
        prompt="iron filings around a bar magnet", seed=42,
    )
    assert "lora" not in g
    assert g["ksampler"]["inputs"]["model"] == ["ckpt", 0]
    assert g["pos"]["inputs"]["clip"] == ["clip", 0]
    assert g["neg"]["inputs"]["clip"] == ["clip", 0]
    assert m["lora"] is None


def test_flux_cfg_is_one_and_guidance_carries_strength():
    # FLUX is guidance-distilled: the KSampler cfg must be ≈1 (prompt strength is
    # the FluxGuidance node), never the SDXL-style >1.
    g, m = wf.assemble_flux_txt2img(
        subject="fysik", image_type="illustration", prompt="x", seed=1,
    )
    assert m["cfg"] == 1.0
    assert g["ksampler"]["inputs"]["cfg"] == 1.0
    assert g["guidance"]["inputs"]["guidance"] == wf.FLUX_DEFAULT_GUIDANCE


def test_flux_no_placeholders_left_after_substitution():
    import json
    for it in ("mikroskop", "illustration"):  # lora + lora-free
        g, _ = wf.assemble_flux_txt2img(
            subject="biologi", image_type=it, prompt="a cell", seed=1,
        )
        s = json.dumps(g)
        assert "${" not in s, f"leftover placeholder in {it} graph"


def test_flux_prompt_and_seed_land_in_graph():
    g, _ = wf.assemble_flux_txt2img(
        subject="fysik", image_type="illustration",
        prompt="UNIQUE_FLUX_PROMPT_TOKEN", seed=987654,
    )
    assert g["pos"]["inputs"]["text"] == "UNIQUE_FLUX_PROMPT_TOKEN"
    assert g["ksampler"]["inputs"]["seed"] == 987654


def test_flux_save_node_outputs_extractable():
    # The graph terminates in a SaveImage so the worker can pull the output.
    g, _ = wf.assemble_flux_txt2img(
        subject="biologi", image_type="mikroskop", prompt="a cell", seed=1,
    )
    assert g["save"]["class_type"] == "SaveImage"
    assert g["save"]["inputs"]["images"] == ["vae", 0]


def test_flux_empty_prompt_rejected():
    with pytest.raises(wf.WorkflowError):
        wf.assemble_flux_txt2img(
            subject="fysik", image_type="illustration", prompt="   ", seed=1,
        )


def test_flux_explicit_empty_lora_dict_renders_lora_free():
    # Passing lora={} (an explicit "no LoRA") must render LoRA-free, even for a
    # type that WOULD map to a FLUX LoRA — the caller's explicit choice wins.
    g, m = wf.assemble_flux_txt2img(
        subject="biologi", image_type="mikroskop",
        prompt="a cell", seed=1, lora={},
    )
    assert "lora" not in g
    assert m["lora"] is None
