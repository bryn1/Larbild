"""Configuration loading for the Lärbild Flask shell.

A clean reimplementation of imagegen's env-driven config convention
(``IMAGE_GEN_*`` → ``LARBILD_*``), carrying over only the shell essentials.
Imports nothing from imagegen and references no restricted module, model, or
feature.

:class:`Config` is instantiated once per :func:`app.create_app` call, so each
boot (and each test) reads a fresh environment. Flask consumes the UPPERCASE
attributes via ``app.config.from_object``.

Later phases extend this:
  * P0.2 — SQLite schema/migrations reads ``DB_PATH``.
  * P0.4 — isolated storage layout reads ``STORAGE_DIR``.
  * P1   — ComfyUI bridge + Redis queue add their own ``LARBILD_*`` knobs.
Add new knobs here; keep them env-overridable with safe LAN defaults.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var. Truthy = 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_secret_key() -> str:
    """Resolve the Flask session secret.

    Precedence:
      1. ``LARBILD_SECRET_KEY`` — a value provided directly (e.g. by systemd).
      2. ``LARBILD_SECRET_PATH`` — file holding a persistent secret. Created
         with ``0600`` perms on first boot if the parent dir is writable.
      3. A fresh random secret for this process only (dev/test/CI). Sessions
         don't survive a restart, which is fine when no path is configured.
    """
    direct = os.environ.get("LARBILD_SECRET_KEY")
    if direct:
        return direct

    secret_path = os.environ.get("LARBILD_SECRET_PATH")
    if secret_path:
        p = Path(secret_path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(48)
            p.write_text(token, encoding="utf-8")
            p.chmod(0o600)
            return token
        except OSError:
            # Fall through to an ephemeral secret rather than refuse to boot.
            pass

    return secrets.token_hex(48)


# Repo root = parent of the app/ package. Defaults live under it so the shell
# runs out-of-the-box in a dev checkout; production overrides via env.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Flask + Lärbild runtime configuration, populated from the environment.

    Flask loads the UPPERCASE attributes; lowercase helpers are ignored.
    """

    def __init__(self) -> None:
        # ── Flask core ────────────────────────────────────────────────
        self.SECRET_KEY = _load_secret_key()
        self.TESTING = _env_bool("LARBILD_TESTING", False)
        self.DEBUG = _env_bool("LARBILD_DEBUG", False)

        # Owner-only LAN tool: lock cookies down, cap request bodies.
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        self.MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MiB

        # ── Lärbild identity ──────────────────────────────────────────
        self.LARBILD_ENV = os.environ.get("LARBILD_ENV", "production")
        # Canonical LAN bind. P0.3's deploy script + larbild.service serve here.
        self.HOST = os.environ.get("LARBILD_HOST", "0.0.0.0")
        self.PORT = int(os.environ.get("LARBILD_PORT", "6868"))

        # ── Foundational paths ────────────────────────────────────────
        # Resolved (not created) here so every phase reads one source of
        # truth. P0.2 owns the DB; P0.4 owns the isolated storage layout.
        self.STORAGE_DIR = Path(
            os.environ.get("LARBILD_STORAGE", str(_REPO_ROOT / "storage"))
        )
        self.DB_PATH = Path(
            os.environ.get("LARBILD_DB_PATH", str(_REPO_ROOT / "data" / "larbild.db"))
        )
        # P0.2 (lb-ah2): run the SQLite migrations on boot. Default on; a test
        # can disable it to avoid filesystem writes when it doesn't touch the DB.
        self.INIT_DB = _env_bool("LARBILD_INIT_DB", True)
        # P10.3 (lb-qtr): full DB↔disk↔library reconcile on boot. Default on; the
        # wiring also defaults it OFF under TESTING so a plain create_app() unit
        # test doesn't walk the storage tree. Env can force it either way.
        self.RECONCILE_STORAGE = _env_bool("LARBILD_RECONCILE_STORAGE", True)
