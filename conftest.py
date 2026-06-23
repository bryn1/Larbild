"""Pytest bootstrap: put the repo root on sys.path.

Lets ``from app import create_app`` resolve regardless of how pytest is
invoked (``pytest`` or ``python -m pytest``) from the repo root.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
