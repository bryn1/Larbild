"""Tests for SFW txt2img workflow assembly + LoRA injection (lb-c2r, P1.1).

Acceptance: the app can build a ComfyUI workflow graph and (its content) is
submittable to vm100:8188; LoRA injection only accepts allowlisted LoRA names
(rejects others with a clear error); a unit/smoke test exercises a minimal
txt2img graph.

The SFW injection guard is the centre of gravity here: a LoRA is always
backend-resolved from (subject, imageType) and re-checked against
baseLora ∪ loraMatrix; an off-allowlist name is refused before any graph is
built.
"""
from __future__ import annotations

import pytest

from app import taxonomy
from app import workflow as wf
from app.comfyui import extract_output_images


# ── the allowlist matches the taxonomy SoT (and the resolve-lora endpoint) ──


def test_allowlist_is_base_union_matrix():
    al = wf.allowlist()
    base_names = {b["name"] for b in taxonomy.BASE_LORA.values()}
    matrix_names = {c["name"] for c in taxonomy.LORA_MATRIX.values()}
    assert al == frozenset(base_names | matrix_names)
    # 8 base + 28 matrix names (some may coincide; allowlist is the union set).
    assert base_names <= al and matrix_names <= al


def test_allowlist_non_empty():
    assert len(wf.allowlist()) > 0


# ── backend-authoritative resolution + validation ──────────────────────────


def test_resolve_and_validate_returns_allowlisted_lora():
    lora = wf.resolve_and_validate_lora("fysik", "illustration")
    assert lora["name"] in wf.allowlist()
    assert lora["file"].endswith(".safetensors")
    assert "strength" in lora


def test_resolve_matches_taxonomy_resolve_lora():
    # The workflow layer must agree with the taxonomy SoT (single source).
    for subject in ("fysik", "historia", "biologi"):
        for it in taxonomy.image_type_slugs():
            got = wf.resolve_and_validate_lora(subject, it)
            expected = taxonomy.resolve_lora(subject, it)
            assert got["name"] == expected["name"]


def test_unknown_subject_rejected():
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_lora("not-a-subject", "illustration")


def test_unknown_image_type_rejected():
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_lora("fysik", "not-a-type")


def test_missing_selection_rejected():
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_lora("", "illustration")
    with pytest.raises(wf.WorkflowError):
        wf.resolve_and_validate_lora("fysik", "")


# ── the SFW injection guard: off-allowlist LoRA is refused ──────────────────


def test_offlist_lora_dict_is_rejected_at_assembly():
    # A crafted pre-resolved LoRA dict naming a name NOT in the allowlist must be
    # refused before any graph is produced — the injection guard. (Uses an
    # obviously-fake off-allowlist name; the guard keys on "not in the SoT set".)
    bad = {"name": "off-allowlist-fake-v9", "strength": 1.0}
    with pytest.raises(wf.LoraNotAllowedError) as ei:
        wf.assemble_txt2img(
            subject="fysik", image_type="illustration",
            prompt="a labelled diagram of a circuit", seed=1, lora=bad,
        )
    assert ei.value.lora_name == "off-allowlist-fake-v9"


def test_offlist_lora_name_carried_in_error():
    # The rejection message names the offending LoRA so the failure is debuggable.
    bad = {"name": "totally-not-real", "strength": 0.5}
    with pytest.raises(wf.LoraNotAllowedError) as ei:
        wf.assemble_txt2img(
            subject="fysik", image_type="foto", prompt="x", seed=1, lora=bad,
        )
    assert "totally-not-real" in str(ei.value)


def test_allowlisted_lora_dict_is_accepted():
    good_name = next(iter(wf.allowlist()))
    good = {"name": good_name, "strength": 0.7}
    workflow, meta = wf.assemble_txt2img(
        subject="fysik", image_type="illustration",
        prompt="a beaker on a stand", seed=7, lora=good,
    )
    assert meta["lora"]["name"] == good_name
    # The injected ComfyUI lora_name is the on-disk file derived from the name.
    assert workflow["lora"]["inputs"]["lora_name"] == wf.lora_filename(good_name)


# ── the assembled graph is a valid, submittable txt2img workflow ────────────


def test_assemble_produces_complete_graph():
    workflow, meta = wf.assemble_txt2img(
        subject="fysik", image_type="illustration",
        prompt="a cross-section of the Earth, labelled", seed=42,
    )
    # Every node class the SDXL still chain needs is present.
    classes = {node["class_type"] for node in workflow.values()}
    assert {
        "CheckpointLoaderSimple", "LoraLoader", "CLIPTextEncode",
        "EmptyLatentImage", "KSampler", "VAEDecode", "SaveImage",
    } <= classes


def test_no_placeholders_left_after_substitution():
    workflow, _ = wf.assemble_txt2img(
        subject="historia", image_type="foto", prompt="a viking longhouse", seed=3,
    )
    import json

    blob = json.dumps(workflow)
    assert "${" not in blob, "an unsubstituted ${VAR} placeholder leaked into the graph"


def test_prompt_and_seed_land_in_the_graph():
    workflow, meta = wf.assemble_txt2img(
        subject="biologi", image_type="mikroskop",
        prompt="a plant cell under a microscope", seed=12345,
    )
    assert workflow["pos"]["inputs"]["text"] == "a plant cell under a microscope"
    assert workflow["ksampler"]["inputs"]["seed"] == 12345
    assert meta["seed"] == 12345
    # seed kept its int type through substitution
    assert isinstance(workflow["ksampler"]["inputs"]["seed"], int)


def test_lora_node_is_wired_between_checkpoint_and_sampler():
    # The LoRA must sit on the model + clip path: ckpt -> lora -> (clip encoders
    # + ksampler). A graph that bypassed the LoRA would silently produce an
    # un-LoRA'd (and thus un-styled / possibly off-distribution) image.
    workflow, _ = wf.assemble_txt2img(
        subject="kemi", image_type="illustration", prompt="a molecule", seed=1,
    )
    assert workflow["lora"]["inputs"]["model"] == ["ckpt", 0]
    assert workflow["lora"]["inputs"]["clip"] == ["ckpt", 1]
    assert workflow["ksampler"]["inputs"]["model"] == ["lora", 0]
    assert workflow["pos"]["inputs"]["clip"] == ["lora", 1]
    assert workflow["neg"]["inputs"]["clip"] == ["lora", 1]


def test_default_negative_prompt_applied():
    workflow, meta = wf.assemble_txt2img(
        subject="fysik", image_type="foto", prompt="a pendulum", seed=1,
    )
    assert workflow["neg"]["inputs"]["text"] == wf.DEFAULT_NEG_PROMPT
    assert meta["neg_prompt"] == wf.DEFAULT_NEG_PROMPT


def test_custom_dimensions_and_steps_respected():
    workflow, meta = wf.assemble_txt2img(
        subject="fysik", image_type="foto", prompt="x", seed=1,
        width=768, height=1152, steps=40, cfg=8.0,
    )
    assert workflow["latent"]["inputs"]["width"] == 768
    assert workflow["latent"]["inputs"]["height"] == 1152
    assert workflow["ksampler"]["inputs"]["steps"] == 40
    assert workflow["ksampler"]["inputs"]["cfg"] == 8.0


def test_empty_prompt_rejected():
    with pytest.raises(wf.WorkflowError):
        wf.assemble_txt2img(
            subject="fysik", image_type="foto", prompt="   ", seed=1,
        )


def test_missing_placeholder_raises_workflow_error():
    # A template referencing an undefined ${VAR} surfaces loudly, not silently.
    with pytest.raises(wf.WorkflowError):
        wf._substitute({"x": "${NOPE}"}, {})


# ── lora_filename mapping ──────────────────────────────────────────────────


def test_lora_filename_appends_suffix():
    assert wf.lora_filename("photoreal-edu-v3") == "photoreal-edu-v3.safetensors"


def test_lora_filename_passthrough_if_already_a_file():
    assert wf.lora_filename("x.safetensors") == "x.safetensors"
    assert wf.lora_filename("y.ckpt") == "y.ckpt"


# ── the SaveImage terminator is discoverable by the client extractor ────────


def test_savefile_output_extractable_by_comfyui_helper():
    # Simulate a finished ComfyUI history for the assembled graph's SaveImage
    # node and confirm the client extractor finds the image — proves the graph's
    # terminator shape matches what comfyui.extract_output_images expects.
    workflow, _ = wf.assemble_txt2img(
        subject="fysik", image_type="illustration", prompt="a circuit", seed=1,
    )
    save_node_id = next(
        nid for nid, n in workflow.items() if n["class_type"] == "SaveImage"
    )
    fake_history = {
        "outputs": {
            save_node_id: {
                "images": [
                    {"filename": "larbild_00001_.png", "subfolder": "", "type": "output"}
                ]
            }
        }
    }
    results = extract_output_images(fake_history)
    assert len(results) == 1
    assert results[0].filename == "larbild_00001_.png"
    assert results[0].type == "output"
