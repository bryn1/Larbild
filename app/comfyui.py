"""ComfyUI HTTP client — forked for Lärbild (lb-c2r).

A SFW-stripped fork of the ``ComfyUIClient`` transport in imagegen's
``sibbis-app/video.py`` (the stills subset — Lärbild P1 renders single images,
not video). Talks to ComfyUI on a render target's ``/prompt`` API directly
(default vm100 ``http://192.168.5.230:8188``; VM100 ufw restricts :8188 to the
Sibbis host). The shape of a render:

    POST  /prompt          <- assembled workflow graph  -> {prompt_id}
    poll  /history/<id>    -> when done, ComfyUI lists the SaveImage outputs
    GET   /view?...        <- download each output PNG

All endpoints are idempotent except ``/prompt`` (which queues real GPU work), so
this client NEVER retries POST /prompt — a duplicate enqueue would cost GPU time
and produce a second image. The other methods may be polled freely.

SFW-only: this is pure HTTP transport to ComfyUI; it carries no model name, no
restricted feature — the workflow it submits is built+validated by
:mod:`app.workflow` (which enforces the SFW LoRA allowlist before anything is
sent here).

Env overrides:
  LARBILD_COMFYUI_URL            default http://192.168.5.230:8188 (vm100)
  LARBILD_COMFYUI_TIMEOUT_S      default 30.0   (per non-/view request)
  LARBILD_COMFYUI_VIEW_TIMEOUT_S default 120.0  (image download)
  LARBILD_COMFYUI_SERVICE_TOKEN  optional X-Service-Token header (back-compat;
                                 ComfyUI :8188 ignores unknown headers)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

__all__ = ["ComfyUIClient", "ComfyResult", "default_comfyui_url"]

SERVICE_TOKEN_HEADER = "X-Service-Token"

DEFAULT_TIMEOUT_S = float(os.environ.get("LARBILD_COMFYUI_TIMEOUT_S", "30.0"))
VIEW_TIMEOUT_S = float(os.environ.get("LARBILD_COMFYUI_VIEW_TIMEOUT_S", "120.0"))


def default_comfyui_url() -> str:
    """The configured ComfyUI base URL (vm100 :8188 by default)."""
    return os.environ.get("LARBILD_COMFYUI_URL", "http://192.168.5.230:8188")


@dataclass
class ComfyResult:
    """One image output listed in a finished ComfyUI history entry."""

    filename: str
    subfolder: str
    type: str  # 'output' / 'temp' / …


class ComfyUIClient:
    """Thin wrapper over ComfyUI's HTTP surface (stills).

    ``service_token``, when set, is sent as the ``X-Service-Token`` header on
    every request (back-compat with a retired auth proxy; ComfyUI :8188 ignores
    unknown headers). Pass a ``session`` (or a mock) to inject transport in
    tests — no live socket is opened until a method is called.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
        service_token: Optional[str] = None,
    ):
        self.base_url = (base_url or default_comfyui_url()).rstrip("/")
        self.timeout = timeout
        self.s = session or requests.Session()
        token = service_token or os.environ.get("LARBILD_COMFYUI_SERVICE_TOKEN")
        if token:
            self.s.headers[SERVICE_TOKEN_HEADER] = token

    def queue_prompt(self, workflow: dict) -> str:
        """POST /prompt — enqueue a workflow graph, return its ``prompt_id``.

        NOT retried: a duplicate enqueue would render a second image.
        """
        r = self.s.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow},
            timeout=self.timeout,
        )
        r.raise_for_status()
        body = r.json()
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI /prompt missing prompt_id: {body!r}")
        return prompt_id

    def history(self, prompt_id: str) -> Optional[dict]:
        """GET /history/<id> — the history entry, or None if still queued/running."""
        r = self.s.get(f"{self.base_url}/history/{prompt_id}", timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        return body.get(prompt_id)

    def queue(self) -> dict:
        """GET /queue — ``{queue_running, queue_pending}``.

        Used to tell "ComfyUI is still working on this prompt" apart from
        "prompt is unknown / lost" during reconcile (lb-qtr, P10.3).
        """
        r = self.s.get(f"{self.base_url}/queue", timeout=self.timeout)
        r.raise_for_status()
        return r.json() or {}

    def view(self, filename: str, subfolder: str, type_: str) -> bytes:
        """GET /view — download one output image's bytes."""
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        r = self.s.get(f"{self.base_url}/view", params=params, timeout=VIEW_TIMEOUT_S)
        r.raise_for_status()
        return r.content


def extract_output_images(history: dict) -> list[ComfyResult]:
    """Every image output from a finished ComfyUI history entry, in save order.

    SaveImage populates ``outputs[<node_id>]["images"]`` with one entry per saved
    image. ComfyUI emits them in order, so we preserve iteration order rather
    than re-sorting on filename. Returns an empty list if there are no outputs.
    """
    out: list[ComfyResult] = []
    outputs = history.get("outputs") or {}
    for _node_id, node_out in outputs.items():
        for img in node_out.get("images") or []:
            out.append(
                ComfyResult(
                    filename=img.get("filename", ""),
                    subfolder=img.get("subfolder", ""),
                    type=img.get("type", "output"),
                )
            )
    return out
