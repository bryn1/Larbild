"""Tests for the VM100 vram_pause fork (lb-c2r, P1.1).

No real SSH and no live GPU: the SSH layer (_run_ssh) and the VRAM poll are
monkeypatched, so eviction/restore logic is exercised purely in-process. The
gpu_lock is stubbed to a no-op context so the pause doesn't touch Redis either.
"""
from __future__ import annotations

import contextlib
import subprocess

import pytest

from app import vram_pause as vp


@contextlib.contextmanager
def _noop_lock(*a, **k):
    yield True


@pytest.fixture(autouse=True)
def _stub_gpu_lock(monkeypatch):
    # Replace the cross-host Redis lock with a no-op so these unit tests don't
    # need Redis. (gpu_lock itself is tested in test_gpu_lock.py.)
    monkeypatch.setattr(vp._gpu_lock, "gpu_lock", _noop_lock)


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_comfyui_holder_is_never_evicted(monkeypatch):
    # nvidia-smi reports ComfyUI + an LLM service. Only the LLM unit must be
    # stopped; ComfyUI must be skipped.
    holders_line = (
        "111\tpython\t/home/a3090/ComfyUI/venv/bin/python main.py\t"
        "0::/system.slice/vm100-comfyui.service\n"
        "222\tllama-server\t/opt/llm/llama-server --model EVA-Qwen2.5-32B\t"
        "0::/system.slice/vm100-imagegen-llm.service\n"
    )
    stopped = []
    started = []

    def _fake_run_ssh(argv, check=True):
        # holder enumeration
        if argv[:2] == ["sh", "-c"]:
            return _Result(stdout=holders_line)
        # systemctl stop/start
        if "systemctl" in argv:
            action, unit = argv[-2], argv[-1]
            if action == "stop":
                stopped.append(unit)
            elif action == "start":
                started.append(unit)
            return _Result()
        # pid-alive checks: report nothing alive (so no kills needed)
        if "--query-compute-apps=pid" in " ".join(argv):
            return _Result(stdout="")
        return _Result()

    monkeypatch.setattr(vp, "_run_ssh", _fake_run_ssh)
    monkeypatch.setattr(vp, "_free_vram_mb", lambda: 20000)  # already plenty free

    with vp.vram_pause(reason="test", threshold_mb=6144, timeout_s=1.0):
        pass

    # the LLM unit was stopped (and restored); ComfyUI's unit was never touched
    assert "vm100-imagegen-llm.service" in stopped
    assert "vm100-comfyui.service" not in stopped
    assert started == ["vm100-imagegen-llm.service"]  # restored exactly what we stopped


def test_units_restored_even_when_body_raises(monkeypatch):
    holders_line = (
        "222\tllama-server\t/opt/llm/llama-server\t"
        "0::/system.slice/vm100-imagegen-llm.service\n"
    )
    started = []

    def _fake_run_ssh(argv, check=True):
        if argv[:2] == ["sh", "-c"]:
            return _Result(stdout=holders_line)
        if "systemctl" in argv:
            if argv[-2] == "start":
                started.append(argv[-1])
            return _Result()
        if "--query-compute-apps=pid" in " ".join(argv):
            return _Result(stdout="")
        return _Result()

    monkeypatch.setattr(vp, "_run_ssh", _fake_run_ssh)
    monkeypatch.setattr(vp, "_free_vram_mb", lambda: 20000)

    with pytest.raises(ValueError):
        with vp.vram_pause(reason="boom", timeout_s=1.0):
            raise ValueError("render failed")

    # finally-block restore still ran despite the body raising
    assert started == ["vm100-imagegen-llm.service"]


def test_auth_failure_surfaces_actionable_error(monkeypatch):
    def _auth_fail(argv, check=True):
        raise vp.VramEvictionAuthError(vp._deploy_help())
    monkeypatch.setattr(vp, "_run_ssh", _auth_fail)

    with pytest.raises(vp.VramEvictionAuthError) as ei:
        with vp.vram_pause(reason="x", timeout_s=1.0):
            pass
    msg = str(ei.value)
    assert "ssh-copy-id" in msg and "NOPASSWD" in msg  # the remediation is inline


def test_vram_timeout_raises(monkeypatch):
    monkeypatch.setattr(vp, "_run_ssh", lambda argv, check=True: _Result(stdout=""))
    monkeypatch.setattr(vp, "_free_vram_mb", lambda: 100)  # never reaches threshold
    with pytest.raises(vp.VramPauseTimeout):
        with vp.vram_pause(reason="x", threshold_mb=6144, timeout_s=0.2, interval_s=0.05):
            pass


def test_manual_holder_is_killed_not_systemctl(monkeypatch):
    # A holder with no owning unit (manual launch, PPID 1) is killed by PID; no
    # systemctl stop is issued for it.
    holders_line = "333\tllama-server\t/opt/manual/llama-server --vlm\t0::/\n"
    kills = []
    systemctls = []
    # PID 333 reported alive once then gone (so a single kill suffices).
    alive_states = iter([True, False])

    def _fake_run_ssh(argv, check=True):
        joined = " ".join(argv)
        if argv[:2] == ["sh", "-c"]:
            return _Result(stdout=holders_line)
        if "systemctl" in argv:
            systemctls.append(argv[-1])
            return _Result()
        if "--query-compute-apps=pid" in joined:
            return _Result(stdout="333" if next(alive_states, False) else "")
        if argv[:2] == ["sudo", "kill"]:
            kills.append(argv)
            return _Result()
        return _Result()

    monkeypatch.setattr(vp, "_run_ssh", _fake_run_ssh)
    monkeypatch.setattr(vp, "_free_vram_mb", lambda: 20000)

    with vp.vram_pause(reason="manual", timeout_s=1.0):
        pass

    assert kills, "a manual GPU holder should have been killed by PID"
    # no service-stop for a unit-less holder, so no restore either
    assert systemctls == []


def test_chat_service_default_is_shared_llm():
    # The unit we pause is the same resident compose LLM imagegen stops.
    assert vp.CHAT_SERVICE == "vm100-imagegen-llm"
