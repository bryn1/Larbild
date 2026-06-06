"""Isolated storage + SFW-only model layout (lb-83n, P0.4).

Verifies the acceptance criteria:
  * output, DB, and SFW model dir paths are config-driven and created on boot;
  * none of those paths overlap (output ⟂ models) or land in an imagegen/NSFW
    tree — assert_isolated fails the boot closed if they would;
  * MODEL_DIR is the single checkpoint/LoRA root (checkpoints/ + loras/);
  * INIT_STORAGE gates the boot-time creation;
  * the Porträtt dir is NOT pre-created (it stays lazy).
"""
from __future__ import annotations

import pytest

from app import create_app
from app import storage


# ── path accessors (pure, derived live from config) ──────────────────────────


def test_accessors_derive_from_config(tmp_path):
    cfg = {
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "mdl",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    assert storage.storage_dir(cfg) == tmp_path / "store"
    assert storage.output_dir(cfg) == tmp_path / "store" / "output"
    assert storage.model_dir(cfg) == tmp_path / "mdl"
    assert storage.checkpoint_dir(cfg) == tmp_path / "mdl" / "checkpoints"
    assert storage.lora_dir(cfg) == tmp_path / "mdl" / "loras"
    assert storage.db_parent(cfg) == tmp_path / "db"


# ── ensure_layout creates the whole tree, idempotently ───────────────────────


def test_ensure_layout_creates_all_roots(tmp_path):
    cfg = {
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "mdl",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    storage.ensure_layout(cfg)
    assert (tmp_path / "store").is_dir()
    assert (tmp_path / "store" / "output").is_dir()
    assert (tmp_path / "mdl" / "checkpoints").is_dir()
    assert (tmp_path / "mdl" / "loras").is_dir()
    assert (tmp_path / "db").is_dir()
    # The DB file itself is NOT created here (app.db.init_db owns that).
    assert not (tmp_path / "db" / "larbild.db").exists()
    # Idempotent — a second call is a no-op, not an error.
    storage.ensure_layout(cfg)
    assert (tmp_path / "mdl" / "loras").is_dir()


# ── assert_isolated: fail-closed on an imagegen / NSFW path ───────────────────


def test_isolation_rejects_imagegen_model_dir(tmp_path):
    cfg = {
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "imagegen-repo" / "models",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    with pytest.raises(storage.StorageIsolationError) as ei:
        storage.assert_isolated(cfg)
    assert "imagegen" in str(ei.value)
    # And nothing was created (assert runs before any mkdir).
    assert not (tmp_path / "imagegen-repo").exists()


def test_isolation_rejects_nsfw_marker_in_path(tmp_path):
    cfg = {
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "lustify-loras",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    with pytest.raises(storage.StorageIsolationError):
        storage.assert_isolated(cfg)


def test_isolation_rejects_db_in_imagegen_tree(tmp_path):
    cfg = {
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "mdl",
        "DB_PATH": tmp_path / "sibbis-app" / "larbild.db",
    }
    with pytest.raises(storage.StorageIsolationError):
        storage.assert_isolated(cfg)


# ── assert_isolated: output ⟂ models (no collision / nesting) ─────────────────


def test_isolation_rejects_output_equals_models(tmp_path):
    shared = tmp_path / "same"
    cfg = {
        "STORAGE_DIR": shared,
        "MODEL_DIR": shared,
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    with pytest.raises(storage.StorageIsolationError) as ei:
        storage.assert_isolated(cfg)
    assert "overlap" in str(ei.value)


def test_isolation_rejects_models_nested_in_storage(tmp_path):
    store = tmp_path / "store"
    cfg = {
        "STORAGE_DIR": store,
        "MODEL_DIR": store / "models",  # nested under the output root
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    with pytest.raises(storage.StorageIsolationError):
        storage.assert_isolated(cfg)


def test_distinct_sibling_roots_pass(tmp_path):
    cfg = {
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "mdl",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    }
    storage.assert_isolated(cfg)  # must not raise


def test_default_config_paths_are_isolated():
    """The shipped repo-relative defaults must not trip the isolation guard.

    Guards against a future path marker false-positiving on the repo location.
    """
    from app.config import Config

    cfg = vars(Config())
    storage.assert_isolated(cfg)  # must not raise


# ── boot integration: create_app builds the layout ───────────────────────────


def test_create_app_builds_layout_on_boot(tmp_path):
    create_app({
        "TESTING": True,
        "INIT_DB": False,  # isolate this test to the storage layout
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "mdl",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    })
    assert (tmp_path / "store" / "output").is_dir()
    assert (tmp_path / "mdl" / "checkpoints").is_dir()
    assert (tmp_path / "mdl" / "loras").is_dir()
    assert (tmp_path / "db").is_dir()
    # Porträtt dir stays lazy — not part of the boot layout.
    assert not (tmp_path / "store" / "portraits").exists()


def test_init_storage_false_skips_creation(tmp_path):
    create_app({
        "TESTING": True,
        "INIT_DB": False,
        "INIT_STORAGE": False,
        "STORAGE_DIR": tmp_path / "store",
        "MODEL_DIR": tmp_path / "mdl",
        "DB_PATH": tmp_path / "db" / "larbild.db",
    })
    assert not (tmp_path / "store").exists()
    assert not (tmp_path / "mdl").exists()


def test_boot_fails_closed_on_non_isolated_model_dir(tmp_path):
    """A misconfigured (imagegen) model dir must crash the boot, not boot dirty."""
    with pytest.raises(storage.StorageIsolationError):
        create_app({
            "TESTING": True,
            "INIT_DB": False,
            "STORAGE_DIR": tmp_path / "store",
            "MODEL_DIR": tmp_path / "imagegen" / "models",
            "DB_PATH": tmp_path / "db" / "larbild.db",
        })
