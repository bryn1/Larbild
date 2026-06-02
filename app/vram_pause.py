"""VRAM-pause helper for VM100 — forked for Lärbild (lb-c2r).

A SFW-stripped fork of imagegen's ``sibbis-app/vram_pause_client.py`` (ig-odj5).
Before a heavy render is submitted to ComfyUI on VM100 (:8188), the resident
compose/chat LLM holds ~10-21 GB of the 24 GB 3090; an SDXL render then OOMs.
This context manager EVICTS whatever non-ComfyUI process holds the 3090, polls
nvidia-smi until free VRAM crosses a threshold, then RESTORES (restarts) exactly
the services it stopped on exit — even when the body raises.

Two coordination layers, outermost → innermost:

  1. **Redis gpu_lock** (cross-host, SHARED ``lock:vm100-3090``): serialises this
     Lärbild caller with imagegen's VM100 worker and any imagegen Sibbis pipeline
     that also goes through a vram-pause. Without it, two pipelines independently
     evict + restore; the first finishes and restarts the LLM while the second is
     still mid-render → ComfyUI OOMs the second.
  2. **threading.Lock** (intra-process): a process-local lock so two
     near-simultaneous Lärbild submits don't both pay the SSH eviction
     round-trip.

The eviction enumerates the ACTUAL GPU compute apps via
``nvidia-smi --query-compute-apps``, identifies ComfyUI by its process path and
NEVER touches it, and for every OTHER holder maps PID → systemd unit via
``/proc/<pid>/cgroup``; a service-managed holder is ``systemctl stop``-ed (which
also disarms its ``Restart=on-failure`` for the pause window), a manual holder
is killed. On exit, exactly the units that were stopped are restarted
(idempotent ``systemctl start``).

SFW-only: the only GPU process this module is *aware of by name* is ComfyUI;
every other holder is evicted generically by PID/unit. No restricted import,
model, or feature is referenced here.

REQUIRED INFRA (Sibbis → VM100 over SSH):
  A. SSH key  lektove@Sibbis → a3090@192.168.5.230  (VM100 is key-auth-only):
         ssh-copy-id -i ~/.ssh/id_ed25519.pub a3090@192.168.5.230
  B. NOPASSWD sudoers on VM100 (/etc/sudoers.d/sibbis-vram-pause, mode 0440):
         a3090 ALL=(root) NOPASSWD: /usr/bin/systemctl stop *, \
             /usr/bin/systemctl start *, /usr/bin/systemctl restart *, \
             /usr/bin/systemctl is-active *, /bin/kill, /bin/kill -9 *
If either is missing the SSH call fails fast (BatchMode=yes) and this module
raises :class:`VramEvictionAuthError` with the exact remediation above — it does
NOT silently proceed to OOM the render.

Env overrides (all optional):
  VRAM_PAUSE_VM100_SSH        default a3090@192.168.5.230
  LARBILD_LLM_SERVICE         the unit to stop to free the 3090
                              (default vm100-imagegen-llm — the SAME resident
                              compose LLM service imagegen stops; Lärbild and
                              imagegen contend for one card, so they pause the
                              same holder). SIBBIS_LLM_SERVICE is honored as a
                              fallback so a host that sets only the shared env
                              still resolves.
  VRAM_PAUSE_THRESHOLD_MB     default 6144
  VRAM_PAUSE_POLL_TIMEOUT_S   default 30.0
  VRAM_PAUSE_POLL_INTERVAL_S  default 0.5
  VRAM_PAUSE_SSH_TIMEOUT_S    default 15.0   (per-ssh command timeout)
  VRAM_PAUSE_COMFYUI_MARKER   default "ComfyUI"  (path substring of the process
                              we must never kill)
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

from . import gpu_lock as _gpu_lock

log = logging.getLogger("larbild.vram_pause")

__all__ = [
    "vram_pause",
    "VramPauseTimeout",
    "VramEvictionAuthError",
    "CHAT_SERVICE",
]


VM100_SSH = os.environ.get("VRAM_PAUSE_VM100_SSH", "a3090@192.168.5.230")
# The unit we stop to free the 3090 — the resident compose LLM, the SAME service
# imagegen stops (one card, two apps). Resolution: LARBILD_LLM_SERVICE wins, then
# the shared SIBBIS_LLM_SERVICE (so a host setting only the cross-app env still
# resolves), else the default. Kept as the module attr CHAT_SERVICE for the
# restore path + tests.
CHAT_SERVICE = os.environ.get(
    "LARBILD_LLM_SERVICE",
    os.environ.get("SIBBIS_LLM_SERVICE", "vm100-imagegen-llm"),
)
THRESHOLD_MB = int(os.environ.get("VRAM_PAUSE_THRESHOLD_MB", str(6 * 1024)))
POLL_TIMEOUT_S = float(os.environ.get("VRAM_PAUSE_POLL_TIMEOUT_S", "30.0"))
POLL_INTERVAL_S = float(os.environ.get("VRAM_PAUSE_POLL_INTERVAL_S", "0.5"))
SSH_TIMEOUT_S = float(os.environ.get("VRAM_PAUSE_SSH_TIMEOUT_S", "15.0"))
# Substring identifying the ComfyUI process we must NEVER evict. Matched against
# both the nvidia-smi process name and the full /proc/<pid>/cmdline.
COMFYUI_MARKER = os.environ.get("VRAM_PAUSE_COMFYUI_MARKER", "ComfyUI")

# Service-managed GPU consumers on VM100, keyed by a cmdline substring → owning
# unit. A *fallback* hint used only when a PID's cgroup can't be read; the
# primary mechanism is the live cgroup lookup in `_unit_from_cgroup`.
SERVICE_HINTS: dict[str, str] = {
    "Qwen2.5-14B-Instruct-abliterated": "vm100-imagegen-llm.service",
    "EVA-Qwen2.5-32B": "vm100-imagegen-llm.service",
}


# Only one Lärbild pause active at a time. Concurrent callers block here so two
# near-simultaneous submits queue rather than racing evict/restore on VM100.
_LOCK = threading.Lock()


class VramPauseTimeout(RuntimeError):
    """nvidia-smi never reported >= threshold free within the deadline."""


class VramEvictionAuthError(RuntimeError):
    """SSH to VM100 failed for an auth/connectivity reason.

    Carries an actionable message naming exactly which infra to deploy (the
    Sibbis→VM100 SSH key + NOPASSWD sudoers line) so the failure is
    self-explanatory in the logs rather than a bare exit-255.
    """


# stderr phrases that mean "could not authenticate/connect" (vs "the remote
# command ran and returned non-zero"). On a match we raise VramEvictionAuthError.
_AUTH_FAIL_PATTERNS = (
    "permission denied",
    "authentication required",
    "publickey",
    "no such identity",
    "host key verification failed",
    "connection refused",
    "connection timed out",
    "could not resolve hostname",
    "operation timed out",
    "network is unreachable",
)

DEPLOY_HELP = (
    "Sibbis cannot reach/authenticate to VM100 ({ssh}). VRAM eviction is "
    "BLOCKED — renders will OOM until this is fixed. Deploy BOTH of the "
    "following (run as lektove on Sibbis / root on VM100):\n"
    "  1. SSH KEY  (lektove@Sibbis -> a3090@VM100):\n"
    "       ssh-copy-id -i ~/.ssh/id_ed25519.pub a3090@192.168.5.230\n"
    "     (VM100 is key-auth-only; append lektove's pubkey to "
    "/home/a3090/.ssh/authorized_keys).\n"
    "  2. NOPASSWD SUDOERS on VM100  "
    "(/etc/sudoers.d/sibbis-vram-pause, mode 0440):\n"
    "       a3090 ALL=(root) NOPASSWD: /usr/bin/systemctl stop *, "
    "/usr/bin/systemctl start *, /usr/bin/systemctl restart *, "
    "/usr/bin/systemctl is-active *, /bin/kill, /bin/kill -9 *\n"
    "     (validate: visudo -cf /etc/sudoers.d/sibbis-vram-pause)."
)


def _deploy_help() -> str:
    return DEPLOY_HELP.format(ssh=VM100_SSH)


def _ssh_command(remote_argv: List[str]) -> List[str]:
    """ssh argv that invokes ``remote_argv`` on VM100.

    BatchMode=yes makes a missing-key failure fast and explicit rather than
    hanging on a password prompt; accept-new auto-trusts the host fingerprint on
    first contact so a freshly imaged VM100 doesn't deadlock the first deploy.
    """
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        VM100_SSH,
        *remote_argv,
    ]


def _looks_like_auth_failure(stderr: str, returncode: int) -> bool:
    """True if ssh stderr/returncode indicate an auth/connect failure.

    ssh uses exit 255 for transport-level errors and forwards the remote
    command's exit code otherwise. 255 + a recognised phrase ⇒ auth/connectivity
    failure; a non-255 non-zero is a legitimate remote-command failure (e.g.
    ``systemctl stop`` of a missing unit) and is NOT an auth error.
    """
    low = (stderr or "").lower()
    if any(pat in low for pat in _AUTH_FAIL_PATTERNS):
        return True
    if returncode == 255:
        return True
    return False


def _run_ssh(
    remote_argv: List[str], *, check: bool = True
) -> subprocess.CompletedProcess:
    """Run ``remote_argv`` on VM100 over SSH, capturing output.

    Raises VramEvictionAuthError (with deploy instructions) on an
    auth/connectivity failure. On a genuine remote-command non-zero exit,
    behaves like ``subprocess.run(check=...)``.
    """
    cmd = _ssh_command(remote_argv)
    try:
        result = subprocess.run(
            cmd, timeout=SSH_TIMEOUT_S, capture_output=True, text=True,
        )
    except FileNotFoundError as e:  # ssh binary missing on Sibbis
        raise VramEvictionAuthError(
            f"`ssh` not found on Sibbis: {e}\n{_deploy_help()}"
        ) from e
    if result.returncode != 0 and _looks_like_auth_failure(
        result.stderr, result.returncode
    ):
        raise VramEvictionAuthError(
            f"SSH to {VM100_SSH} failed (rc={result.returncode}): "
            f"{(result.stderr or '').strip()}\n{_deploy_help()}"
        )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr,
        )
    return result


def _systemctl(action: str, service: Optional[str] = None) -> None:
    """``sudo systemctl <action> <service>`` on VM100. Raises on non-zero.

    When ``service`` is omitted it targets CHAT_SERVICE (the one-arg contract the
    restore path + tests rely on).
    """
    svc = service if service is not None else CHAT_SERVICE
    _run_ssh(["sudo", "systemctl", action, svc], check=True)


# ── GPU-holder enumeration + eviction ─────────────────────────────────

# A compute-app holder is (pid, process_name, full_cmdline, owning_unit_or_None).


def _query_gpu_holders() -> List[Tuple[int, str, str, Optional[str]]]:
    """Enumerate GPU compute apps on VM100, with cmdline + owning unit.

    One SSH round-trip runs nvidia-smi, then for each PID reads
    ``/proc/<pid>/cmdline`` + ``/proc/<pid>/cgroup``, emitting tab-separated
    ``pid\\tname\\tcmdline\\tcgroup`` records. A single remote shell keeps the
    inspection atomic (no per-PID SSH storm) and lets us classify ComfyUI vs
    evictable holders deterministically.
    """
    remote = (
        "for row in $(nvidia-smi --query-compute-apps=pid "
        "--format=csv,noheader,nounits 2>/dev/null); do "
        "pid=$row; "
        "name=$(cat /proc/$pid/comm 2>/dev/null); "
        "cmd=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null); "
        "cg=$(cat /proc/$pid/cgroup 2>/dev/null | head -n1); "
        "printf '%s\\t%s\\t%s\\t%s\\n' \"$pid\" \"$name\" \"$cmd\" \"$cg\"; "
        "done"
    )
    result = _run_ssh(["sh", "-c", remote], check=False)
    holders: List[Tuple[int, str, str, Optional[str]]] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            parts += [""] * (4 - len(parts))
        pid_s, name, cmdline, cgroup = parts[0], parts[1], parts[2], parts[3]
        try:
            pid = int(pid_s.strip())
        except ValueError:
            continue
        unit = _unit_from_cgroup(cgroup)
        if unit is None:
            unit = _unit_from_hints(cmdline)
        holders.append((pid, name.strip(), cmdline.strip(), unit))
    return holders


_CGROUP_UNIT_RE = re.compile(r"/([A-Za-z0-9_.@-]+\.service)\b")


def _unit_from_cgroup(cgroup_line: str) -> Optional[str]:
    """Parse the owning ``.service`` unit from a /proc/<pid>/cgroup line.

    cgroup v2 lines look like ``0::/system.slice/vm100-comfyui.service``. Returns
    the unit name or None for a process not under a service cgroup (manual
    launch, PPID 1).
    """
    if not cgroup_line:
        return None
    m = _CGROUP_UNIT_RE.search(cgroup_line)
    return m.group(1) if m else None


def _unit_from_hints(cmdline: str) -> Optional[str]:
    """Best-effort unit lookup from SERVICE_HINTS when the cgroup is unreadable."""
    for marker, unit in SERVICE_HINTS.items():
        if marker in cmdline:
            return unit
    return None


def _is_comfyui(name: str, cmdline: str) -> bool:
    """True if this holder is the ComfyUI process we must NEVER evict.

    Matched on the COMFYUI_MARKER substring (default ``ComfyUI``) in either the
    nvidia-smi process name or the full cmdline/path.
    """
    marker = COMFYUI_MARKER
    return marker in (name or "") or marker in (cmdline or "")


def _kill_pid(pid: int, *, force: bool = False) -> None:
    """``sudo kill [-9] <pid>`` on VM100. Best-effort; non-fatal on failure.

    Killing an already-dead PID returns non-zero ("No such process"); we treat
    that as success (the goal — process gone — is met). A genuine SSH auth
    failure still raises via _run_ssh.
    """
    argv = ["sudo", "kill"]
    if force:
        argv.append("-9")
    argv.append(str(pid))
    res = _run_ssh(argv, check=False)
    if res.returncode != 0:
        log.debug(
            "kill%s %d rc=%d stderr=%s (treating as gone)",
            " -9" if force else "", pid, res.returncode,
            (res.stderr or "").strip(),
        )


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` still appears among VM100 GPU compute apps."""
    res = _run_ssh(
        ["nvidia-smi", "--query-compute-apps=pid",
         "--format=csv,noheader,nounits"],
        check=False,
    )
    for line in (res.stdout or "").splitlines():
        s = line.strip()
        if s and s.isdigit() and int(s) == pid:
            return True
    return False


def _evict_holders() -> List[str]:
    """Evict every non-ComfyUI GPU holder on VM100. Return units stopped.

    For each holder that is NOT ComfyUI: if service-managed, ``systemctl stop``
    the unit (disarming its auto-restart for the pause window) and kill the PID
    if it lingers; if manual (no unit), kill the PID, escalating to ``kill -9``.
    ComfyUI is skipped entirely. Returns the de-duplicated list of unit names
    stopped, for the caller to restart in ``finally``. Raises
    VramEvictionAuthError if SSH cannot authenticate to VM100.
    """
    holders = _query_gpu_holders()
    stopped_units: List[str] = []
    for pid, name, cmdline, unit in holders:
        if _is_comfyui(name, cmdline):
            log.info(
                "vram_pause skip ComfyUI pid=%d (never evicted) name=%s", pid, name,
            )
            continue
        log.info(
            "vram_pause evict pid=%d name=%s unit=%s", pid, name, unit or "manual",
        )
        if unit:
            try:
                _systemctl("stop", unit)
                if unit not in stopped_units:
                    stopped_units.append(unit)
            except subprocess.CalledProcessError as e:
                log.warning(
                    "vram_pause systemctl stop %s failed rc=%s — "
                    "falling back to kill pid=%d",
                    unit, getattr(e, "returncode", "?"), pid,
                )
            if _pid_alive(pid):
                _kill_pid(pid)
                if _pid_alive(pid):
                    _kill_pid(pid, force=True)
        else:
            _kill_pid(pid)
            if _pid_alive(pid):
                _kill_pid(pid, force=True)
    return stopped_units


def _free_vram_mb() -> Optional[int]:
    """First-GPU free memory on VM100 in MiB. None on any failure.

    A None return means no signal — the caller keeps polling rather than
    treating it as "ready", so a transient ssh blip doesn't yield a false ready.
    """
    cmd = _ssh_command([
        "nvidia-smi",
        "--query-gpu=memory.free",
        "--format=csv,noheader,nounits",
    ])
    try:
        result = subprocess.run(
            cmd, check=True, timeout=SSH_TIMEOUT_S, capture_output=True, text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    first = (result.stdout or "").strip().splitlines()[:1]
    if not first:
        return None
    try:
        return int(first[0].strip())
    except ValueError:
        return None


def _wait_for_vram(
    threshold_mb: int, timeout_s: float, interval_s: float,
) -> int:
    deadline = time.monotonic() + timeout_s
    last_seen: Optional[int] = None
    while True:
        free_mb = _free_vram_mb()
        if free_mb is not None:
            last_seen = free_mb
            if free_mb >= threshold_mb:
                return free_mb
        if time.monotonic() >= deadline:
            raise VramPauseTimeout(
                f"VM100 free VRAM did not reach {threshold_mb} MiB "
                f"within {timeout_s}s (last observed: {last_seen} MiB)"
            )
        time.sleep(interval_s)


@contextmanager
def vram_pause(
    reason: str = "",
    *,
    threshold_mb: int = THRESHOLD_MB,
    timeout_s: float = POLL_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
) -> Iterator[int]:
    """Evict GPU holders on VM100, wait for VRAM, restore on exit.

    Wraps the SHARED Redis :func:`app.gpu_lock.gpu_lock` (cross-host mutual
    exclusion with imagegen) around an intra-process ``threading.Lock`` and the
    SSH evict/restore. Yields the observed free-VRAM MiB once the threshold is
    met. On exit, EXACTLY the units that were stopped are restarted
    (``systemctl start`` is idempotent). If SSH cannot authenticate to VM100,
    eviction raises VramEvictionAuthError with deploy instructions BEFORE the
    body runs — a loud, actionable failure rather than a silent OOM.

    Manually-killed (unit-less) processes are intentionally NOT relaunched here:
    we don't know their original argv; whoever started them owns their lifecycle.
    """
    with _gpu_lock.gpu_lock(reason=f"vram_pause: {reason}"):
        _LOCK.acquire()
        log.info("vram_pause acquire reason=%s threshold=%d", reason, threshold_mb)
        stopped_units: List[str] = []
        try:
            stopped_units = _evict_holders()
            free_mb = _wait_for_vram(threshold_mb, timeout_s, interval_s)
            log.info(
                "vram_pause ready free_mb=%d reason=%s stopped=%s",
                free_mb, reason, stopped_units,
            )
            yield free_mb
        finally:
            # Restart EXACTLY the units we stopped. Per-unit failures are logged
            # LOUD but not re-raised — re-raising in finally would mask any
            # in-flight body exception (e.g. VramPauseTimeout or a render error).
            for unit in stopped_units:
                try:
                    _systemctl("start", unit)
                    log.info("vram_pause restart=ok unit=%s reason=%s", unit, reason)
                except (
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                    VramEvictionAuthError,
                ) as e:
                    log.error(
                        "vram_pause restart=FAILED reason=%s unit=%s: %s — "
                        "MANUAL INTERVENTION REQUIRED (sudo systemctl start %s "
                        "on VM100)",
                        reason, unit, e, unit,
                    )
            _LOCK.release()
