"""Full three-way reconcile-on-boot — DB ↔ disk ↔ library (lb-qtr, P10.3).

Verifies the build-plan §P10 'reconcile-on-boot' completion: on startup the app
reconciles ``library`` rows against on-disk files and the auto-sort buckets.

Acceptance coverage:
  * dangling library row (file absent) detected + flagged (soft, not deleted)
  * orphaned file (no row) detected — report only
  * mis-sorted asset (wrong taxonomy bucket) detected
  * idempotent (re-run = same result, no extra writes)
  * does-NOT-delete-by-default (rows + files survive a reconcile)
  * self-heals: a re-appeared file clears the flag; converges DB→disk on restart
  * bounded/skippable so a big tree never blocks boot
  * wired into create_app after the job reconcile

No live GPU, no real Redis (the storage reconcile touches neither).
"""
from __future__ import annotations

import pytest

from app import reconcile
from app.db import connect, init_db, new_id


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def storage(tmp_path):
    """A DB + an isolated STORAGE_DIR with the output/ tree, like production."""
    db_path = tmp_path / "reconcile.db"
    init_db(db_path)  # runs migrations → library table + missing_since column
    storage_dir = tmp_path / "storage"
    (storage_dir / "output").mkdir(parents=True)
    return storage_dir


def _add_library_row(
    *, subject="fysik", area="ljud", image_type="foto",
    kind="bild", video_type=None, file_path=None, write_file=True,
    storage_dir=None, lib_id=None,
):
    """Insert a library row and (optionally) create its backing file on disk.

    Returns (lib_id, file_path). When ``file_path`` is omitted it is derived from
    the auto-sort layout the worker uses, so a row created with defaults is
    'correctly sorted' and its file is present — the healthy baseline.
    """
    lib_id = lib_id or new_id()
    if file_path is None:
        bucket = reconcile.expected_bucket(kind, image_type, video_type)
        file_path = f"output/{subject}/{area}/{bucket}/{lib_id}.png"
    with connect() as conn:
        conn.execute(
            "INSERT INTO library (id, kind, subject, area, image_type, "
            "video_type, title, file_path) VALUES (?, ?, ?, ?, ?, ?, 'T', ?)",
            (lib_id, kind, subject, area, image_type, video_type, file_path),
        )
    if write_file and storage_dir is not None:
        abs_path = storage_dir / file_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"\x89PNG fake bytes")
    return lib_id, file_path


def _missing_since(lib_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT missing_since FROM library WHERE id = ?", (lib_id,)
        ).fetchone()
    return row["missing_since"]


def _row_exists(lib_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM library WHERE id = ?", (lib_id,)
        ).fetchone()
    return row is not None


# ── dangling library row (DB row, no file on disk) ──────────────────────────


def test_dangling_row_detected_and_flagged(storage):
    """A library row whose file is missing is detected + soft-flagged missing."""
    lib_id, _ = _add_library_row(storage_dir=storage, write_file=False)
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert lib_id in report.dangling
    assert lib_id in report.flagged_missing
    # the flag is persisted (a soft marker, not a delete)
    assert _missing_since(lib_id) is not None
    assert not report.consistent


def test_dangling_row_is_not_deleted(storage):
    """CONSERVATIVE: the dangling row (and its metadata) survives — never purged."""
    lib_id, _ = _add_library_row(storage_dir=storage, write_file=False)
    reconcile.reconcile_storage(storage, scan_orphans=False)
    assert _row_exists(lib_id), "reconcile must NOT delete a dangling library row"


def test_reappeared_file_clears_flag(storage):
    """Self-heal: once the file is back, the missing flag is cleared on next run."""
    lib_id, file_path = _add_library_row(storage_dir=storage, write_file=False)
    reconcile.reconcile_storage(storage, scan_orphans=False)
    assert _missing_since(lib_id) is not None
    # the file reappears (e.g. a sync caught up)
    abs_path = storage / file_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"back")
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert lib_id in report.cleared_missing
    assert _missing_since(lib_id) is None
    # and it's no longer reported dangling
    assert lib_id not in report.dangling


def test_soft_deleted_row_is_ignored(storage):
    """A soft-deleted row whose file is gone is NOT flagged dangling (it's meant to be gone)."""
    lib_id, _ = _add_library_row(storage_dir=storage, write_file=False)
    with connect() as conn:
        conn.execute(
            "UPDATE library SET deleted_at = datetime('now') WHERE id = ?", (lib_id,)
        )
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert lib_id not in report.dangling
    assert _missing_since(lib_id) is None


# ── orphaned file (file under output/, no library row) ──────────────────────


def test_orphaned_file_detected(storage):
    """A file under output/ with no library row is reported as orphaned."""
    orphan = storage / "output" / "kemi" / "vatten" / "foto" / "loose.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    report = reconcile.reconcile_storage(storage, scan_orphans=True)
    assert "output/kemi/vatten/foto/loose.png" in report.orphaned_files
    assert not report.consistent


def test_orphaned_file_is_not_deleted(storage):
    """CONSERVATIVE: an orphaned file is reported, never removed from disk."""
    orphan = storage / "output" / "kemi" / "vatten" / "foto" / "loose.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    reconcile.reconcile_storage(storage, scan_orphans=True)
    assert orphan.exists(), "reconcile must NOT delete an orphaned file"


def test_known_file_is_not_an_orphan(storage):
    """A file that DOES have a library row is not reported as orphaned."""
    _add_library_row(storage_dir=storage, write_file=True)
    report = reconcile.reconcile_storage(storage, scan_orphans=True)
    assert report.orphaned_files == []


def test_orphan_scan_skippable(storage):
    """scan_orphans=False runs the DB pass only — no directory walk, no orphans."""
    orphan = storage / "output" / "kemi" / "vatten" / "foto" / "loose.png"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert report.orphan_scan_skipped is True
    assert report.orphaned_files == []  # the walk never ran


def test_orphan_scan_is_capped_so_boot_is_not_blocked(storage):
    """The orphan walk hard-caps at max_scan files (boot-speed guarantee)."""
    base = storage / "output" / "fysik" / "ljud" / "foto"
    base.mkdir(parents=True, exist_ok=True)
    for i in range(8):
        (base / f"f{i}.png").write_bytes(b"x")
    report = reconcile.reconcile_storage(storage, scan_orphans=True, max_scan=3)
    assert report.orphan_scan_capped is True
    # stopped early: never enumerated more than the cap
    assert len(report.orphaned_files) <= 3


# ── mis-sorted asset (file in the wrong taxonomy bucket) ────────────────────


def test_mis_sorted_asset_detected(storage):
    """A library file not under its subject/area/imageType bucket is flagged mis-sorted."""
    # Row says fysik/ljud/foto, but the file actually lives under .../diagram/.
    lib_id, _ = _add_library_row(
        storage_dir=storage, image_type="foto",
        file_path="output/fysik/ljud/diagram/x.png", write_file=True,
    )
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert lib_id in report.mis_sorted
    assert not report.consistent


def test_correctly_sorted_asset_not_flagged(storage):
    """A correctly auto-sorted file is not reported as mis-sorted."""
    lib_id, _ = _add_library_row(storage_dir=storage, write_file=True)
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert lib_id not in report.mis_sorted
    assert report.mis_sorted == []


def test_film_bucket_is_recognised(storage):
    """A film row correctly sorted under .../film/ is NOT mis-sorted (bucket='film')."""
    lib_id, _ = _add_library_row(
        storage_dir=storage, kind="film", image_type=None, video_type="lektionsfilm",
        subject="biologi", area="celler", write_file=True,
    )
    report = reconcile.reconcile_storage(storage, scan_orphans=False)
    assert lib_id not in report.mis_sorted


def test_mis_sorted_asset_is_not_moved_or_deleted(storage):
    """CONSERVATIVE: a mis-sorted file is reported, never moved or deleted."""
    lib_id, file_path = _add_library_row(
        storage_dir=storage, file_path="output/fysik/ljud/diagram/x.png",
        write_file=True,
    )
    reconcile.reconcile_storage(storage, scan_orphans=False)
    assert (storage / file_path).exists(), "mis-sorted file must stay put"
    assert _row_exists(lib_id)


# ── idempotency + consistency ───────────────────────────────────────────────


def test_reconcile_is_idempotent(storage):
    """Re-running on an unchanged tree gives the same report + makes no new writes."""
    dangling, _ = _add_library_row(storage_dir=storage, write_file=False)
    _add_library_row(storage_dir=storage, write_file=True)  # a healthy row

    first = reconcile.reconcile_storage(storage, scan_orphans=True)
    assert dangling in first.flagged_missing  # stamped on the first run

    second = reconcile.reconcile_storage(storage, scan_orphans=True)
    # same detection result...
    assert second.dangling == first.dangling
    assert second.mis_sorted == first.mis_sorted
    assert second.orphaned_files == first.orphaned_files
    # ...but the second run does NOT re-stamp the already-flagged row (no write).
    assert second.flagged_missing == []
    assert second.cleared_missing == []


def test_clean_tree_is_consistent(storage):
    """A tree where every row's file is present + correctly sorted reports clean."""
    _add_library_row(storage_dir=storage, write_file=True)
    _add_library_row(storage_dir=storage, subject="kemi", area="vatten", write_file=True)
    report = reconcile.reconcile_storage(storage, scan_orphans=True)
    assert report.consistent
    assert report.dangling == [] and report.orphaned_files == [] and report.mis_sorted == []


def test_best_effort_never_raises_on_db_error(tmp_path):
    """A reconcile error is captured in the report, never raised (can't block boot)."""
    # Point at a storage dir but never init_db → connect() will fail; reconcile
    # must swallow it and return a report carrying the error.
    from app import db as _db

    _db._DB_PATH = tmp_path / "does-not-exist" / "missing.db"
    report = reconcile.reconcile_storage(tmp_path / "storage")
    assert report.error is not None
    # nothing detected because the pass aborted, but no exception escaped
    assert report.dangling == []


# ── wired into create_app (the boot path) ───────────────────────────────────


def test_create_app_runs_storage_reconcile(tmp_path):
    """create_app() runs the full storage reconcile after the job reconcile."""
    from app import create_app

    db_path = tmp_path / "boot.db"
    init_db(db_path)
    storage_dir = tmp_path / "storage"
    (storage_dir / "output").mkdir(parents=True)
    # a dangling row the boot reconcile should flag
    dangling, _ = _add_library_row(storage_dir=storage_dir, write_file=False)

    create_app({
        "DB_PATH": db_path,
        "STORAGE_DIR": storage_dir,
        "RECONCILE_STORAGE": True,     # force on (TESTING would otherwise skip it)
        "TESTING": True,
    })
    # the boot reconcile flagged the dangling row but did NOT delete it
    assert _missing_since(dangling) is not None
    assert _row_exists(dangling)


def test_create_app_skips_storage_reconcile_when_disabled(tmp_path):
    """RECONCILE_STORAGE=False → the storage pass is skipped (no flag written)."""
    from app import create_app

    db_path = tmp_path / "boot.db"
    init_db(db_path)
    storage_dir = tmp_path / "storage"
    (storage_dir / "output").mkdir(parents=True)
    dangling, _ = _add_library_row(storage_dir=storage_dir, write_file=False)

    create_app({
        "DB_PATH": db_path,
        "STORAGE_DIR": storage_dir,
        "RECONCILE_STORAGE": False,
        "TESTING": True,
    })
    # disabled → the dangling row was never flagged
    assert _missing_since(dangling) is None
