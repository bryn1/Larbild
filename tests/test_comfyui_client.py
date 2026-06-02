"""Tests for the ComfyUI HTTP client (lb-c2r, P1.1).

The client is exercised against a MOCK requests.Session — NO live GPU/network
call is made. This is deliberate (CRITICAL constraint): a large imagegen sweep
is on both GPUs, so the bridge is built + tested with a mocked ComfyUI; the
first real render on vm100 is deferred (lb-ocx).
"""
from __future__ import annotations

import pytest

from app import comfyui
from app.comfyui import ComfyUIClient, extract_output_images


class _FakeResponse:
    def __init__(self, *, json_body=None, content=b"", status=200):
        self._json = json_body
        self.content = content
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Records calls + returns scripted responses. No socket is opened."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.post_response: _FakeResponse | None = None
        self.get_responses: dict[str, _FakeResponse] = {}

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.post_response

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        # match by URL suffix path
        for suffix, resp in self.get_responses.items():
            if url.endswith(suffix) or suffix in url:
                return resp
        return _FakeResponse(json_body={})


def test_queue_prompt_posts_workflow_and_returns_prompt_id():
    sess = _FakeSession()
    sess.post_response = _FakeResponse(json_body={"prompt_id": "abc-123"})
    client = ComfyUIClient("http://vm100:8188", session=sess)

    workflow = {"save": {"class_type": "SaveImage", "inputs": {}}}
    pid = client.queue_prompt(workflow)

    assert pid == "abc-123"
    method, url, kw = sess.calls[0]
    assert method == "POST" and url == "http://vm100:8188/prompt"
    # workflow is wrapped under {"prompt": ...} per ComfyUI's API
    assert kw["json"] == {"prompt": workflow}


def test_queue_prompt_missing_id_raises():
    sess = _FakeSession()
    sess.post_response = _FakeResponse(json_body={})  # no prompt_id
    client = ComfyUIClient("http://vm100:8188", session=sess)
    with pytest.raises(RuntimeError):
        client.queue_prompt({"x": 1})


def test_history_returns_entry_for_prompt_id():
    sess = _FakeSession()
    sess.get_responses["/history/p1"] = _FakeResponse(
        json_body={"p1": {"outputs": {"9": {"images": []}}}}
    )
    client = ComfyUIClient("http://vm100:8188", session=sess)
    entry = client.history("p1")
    assert entry == {"outputs": {"9": {"images": []}}}


def test_history_none_when_still_running():
    sess = _FakeSession()
    sess.get_responses["/history/p1"] = _FakeResponse(json_body={})  # not done yet
    client = ComfyUIClient("http://vm100:8188", session=sess)
    assert client.history("p1") is None


def test_view_returns_image_bytes():
    sess = _FakeSession()
    sess.get_responses["/view"] = _FakeResponse(content=b"\x89PNG fake bytes")
    client = ComfyUIClient("http://vm100:8188", session=sess)
    data = client.view("larbild_00001_.png", "", "output")
    assert data == b"\x89PNG fake bytes"


def test_queue_returns_running_pending():
    sess = _FakeSession()
    sess.get_responses["/queue"] = _FakeResponse(
        json_body={"queue_running": [], "queue_pending": []}
    )
    client = ComfyUIClient("http://vm100:8188", session=sess)
    q = client.queue()
    assert "queue_running" in q and "queue_pending" in q


def test_service_token_header_set_when_provided():
    sess = _FakeSession()
    ComfyUIClient("http://vm100:8188", session=sess, service_token="tok")
    assert sess.headers.get("X-Service-Token") == "tok"


def test_base_url_default_from_env(monkeypatch):
    monkeypatch.setenv("LARBILD_COMFYUI_URL", "http://elsewhere:8188")
    assert comfyui.default_comfyui_url() == "http://elsewhere:8188"


def test_base_url_trailing_slash_stripped():
    client = ComfyUIClient("http://vm100:8188/", session=_FakeSession())
    assert client.base_url == "http://vm100:8188"


# ── extract_output_images ──────────────────────────────────────────────────


def test_extract_images_preserves_save_order():
    history = {
        "outputs": {
            "9": {
                "images": [
                    {"filename": "larbild_00001_.png", "subfolder": "", "type": "output"},
                    {"filename": "larbild_00002_.png", "subfolder": "", "type": "output"},
                ]
            }
        }
    }
    results = extract_output_images(history)
    assert [r.filename for r in results] == ["larbild_00001_.png", "larbild_00002_.png"]


def test_extract_images_empty_when_no_outputs():
    assert extract_output_images({"outputs": {}}) == []
    assert extract_output_images({}) == []


# ── end-to-end smoke against the mock (submit → poll → extract) ─────────────


def test_full_render_cycle_mocked():
    """A complete submit→poll→download cycle against a mocked ComfyUI.

    Proves the client wiring end-to-end with NO live GPU: queue a graph, poll
    history until 'done', extract the SaveImage output, fetch its bytes.
    """
    sess = _FakeSession()
    sess.post_response = _FakeResponse(json_body={"prompt_id": "smoke-1"})
    # first history poll: still running (empty); second: done with one image
    polls = iter([
        _FakeResponse(json_body={}),  # running
        _FakeResponse(json_body={
            "smoke-1": {
                "outputs": {"9": {"images": [
                    {"filename": "larbild_00001_.png", "subfolder": "", "type": "output"}
                ]}}
            }
        }),
    ])

    def _get(url, **kw):
        sess.calls.append(("GET", url, kw))
        if "/history/" in url:
            return next(polls)
        if "/view" in url:
            return _FakeResponse(content=b"PNGDATA")
        return _FakeResponse(json_body={})

    sess.get = _get  # type: ignore[assignment]
    client = ComfyUIClient("http://vm100:8188", session=sess)

    pid = client.queue_prompt({"save": {"class_type": "SaveImage", "inputs": {}}})
    assert pid == "smoke-1"

    # poll until done
    entry = client.history(pid)
    assert entry is None  # first poll: running
    entry = client.history(pid)
    assert entry is not None

    images = extract_output_images(entry)
    assert len(images) == 1
    data = client.view(images[0].filename, images[0].subfolder, images[0].type)
    assert data == b"PNGDATA"
