"""Pytest bootstrap: put the repo root on sys.path.

Lets ``from app import create_app`` resolve regardless of how pytest is
invoked (``pytest`` or ``python -m pytest``) from the repo root.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Default the edu-prompt 14B composer OFF under the test suite so the render
# path uses the deterministic template fallback (no network). The composer lives
# on VM100:8091 (reachable only from the Sibbis app host); a test box would
# otherwise block ~30s per render on an unreachable socket. Tests that exercise
# the LLM path opt in explicitly (use_llm=True + a patched/stubbed HTTP call).
# setdefault, so a test/CI that WANTS the real composer can still export =1.
os.environ.setdefault("LARBILD_EDU_LLM_ENABLE", "0")
