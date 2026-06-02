"""Full three-way reconcile-on-boot — DB ↔ disk ↔ library (lb-qtr, P10.3).

The partial reconcile (:func:`app.jobs.reconcile_stuck_jobs`) already settles
orphaned ``running`` jobs on boot. This completes the build-plan §P10 slice: a
restart self-heals the *library* index against on-disk reality, so the Bibliotek
view and the auto-sort layout never silently drift from what is actually on disk.

It checks three kinds of inconsistency and reports a structured
:class:`ReconcileReport`:

1. **Dangling library row** — a ``library`` row whose ``file_path`` does NOT
   exist under ``STORAGE_DIR``. The file vanished (manual delete, a botched
   sync, a half-written render). We FLAG it: stamp ``missing_since`` (the soft
   mirror of ``deleted_at``) so the UI can surface "needs review". We never
   delete the row — the metadata is the only remaining record and the bytes may
   be recoverable. If a previously-flagged row's file has reappeared, we CLEAR
   the flag (self-healing → the DB converges to disk on restart).

2. **Orphaned file** — a file under ``STORAGE_DIR/output/`` with no matching
   ``library`` row (matched by the STORAGE_DIR-relative ``file_path``). Reported
   only (and capped — see ``max_scan``). We do NOT auto-index it (we can't
   reconstruct its taxonomy/provenance safely) and we do NOT delete it; the
   owner decides. Re-index is an opt-in follow-up, not a boot default.

3. **Mis-sorted asset** — a ``library`` row whose on-disk path does NOT match the
   subject/area/imageType (or ``film`` for video) auto-sort bucket the workers
   write to. The file is present, just in the "wrong" directory for its taxonomy
   keys (e.g. a row was re-tagged after the file landed). Reported only — moving
   a user's file on boot is exactly the kind of destructive surprise the build
   plan forbids; surfacing it lets the owner re-file deliberately.

Design guarantees (all from the bead + build plan):

* **Conservative.** Logs + flags + a status. The ONLY DB write is toggling the
  nullable ``missing_since`` flag on/off; no row or file is ever deleted or moved.
* **Idempotent.** Re-running on an unchanged tree produces the same report and
  makes no further writes (a row already flagged ``missing_since`` is detected
  again but not re-stamped; a healthy row is left NULL).
* **Fast / non-blocking.** The dangling+mis-sort pass is one indexed SELECT over
  the (already small, single-teacher) library table. The orphan-file pass walks
  ``output/`` but is (a) skippable via ``RECONCILE_SCAN_ORPHANS`` / the
  ``scan_orphans`` arg and (b) hard-capped at ``max_scan`` files so a huge tree
  can never stall boot. Best-effort: any error is logged, never raised, so a
  reconcile hiccup can't stop the app from starting.

SFW-only: pure consistency bookkeeping over the SFW library + storage; no
restricted import, model, or reference.

Env overrides:
  LARBILD_RECONCILE_STORAGE    default on  — run the full storage reconcile on
                               boot at all. Off → skip entirely (the job
                               reconcile still runs; this only gates the
                               DB↔disk↔library pass).
  LARBILD_RECONCILE_SCAN_ORPHANS  default on — include the orphan-file walk.
                               Off → only the dangling + mis-sort DB pass runs
                               (no directory scan), for very large trees.
  LARBILD_RECONCILE_MAX_SCAN   default 5000 — cap the orphan-file walk at this
                               many files so boot is never blocked by a huge
                               directory scan.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .db import connect, now_iso

log = logging.getLogger("larbild.reconcile")

__all__ = [
    "ReconcileReport",
    "reconcile_storage",
    "expected_bucket",
    "DEFAULT_MAX_SCAN",
]

# Cap the orphan-file walk so a pathologically large output/ tree can never block
# boot. Overridable via LARBILD_RECONCILE_MAX_SCAN. The single-teacher tool will
# never come close, but the cap is the hard guarantee the build plan asks for.
DEFAULT_MAX_SCAN = int(os.environ.get("LARBILD_RECONCILE_MAX_SCAN", "5000"))

# The auto-sort bucket for a video row is the literal "film" dir (app.film);
# a still uses its imageType, falling back to "övrigt" exactly like the worker
# (app.render_worker._relative_output_path) so detection matches what was written.
_FILM_BUCKET = "film"
_FALLBACK_BUCKET = "övrigt"


@dataclass
class ReconcileReport:
    """Structured result of a storage reconcile pass — what was found + done.

    Counts are the headline; the id/path lists name the specific rows/files for
    logging or an admin view. ``flagged_missing`` / ``cleared_missing`` are the
    only entries that correspond to a DB write (toggling ``missing_since``);
    everything else is detection-only (reported, never mutated).
    """

    # Dangling library rows (DB row, file absent under STORAGE_DIR).
    dangling: list[str] = field(default_factory=list)        # all detected, this run
    flagged_missing: list[str] = field(default_factory=list)  # newly stamped this run
    cleared_missing: list[str] = field(default_factory=list)  # file reappeared → un-flagged
    # Orphaned files (file under output/, no library row). Reported only.
    orphaned_files: list[str] = field(default_factory=list)
    orphan_scan_capped: bool = False    # hit max_scan before finishing the walk
    orphan_scan_skipped: bool = False   # scan_orphans was off
    # Mis-sorted assets (library file not in its taxonomy auto-sort bucket).
    mis_sorted: list[str] = field(default_factory=list)
    # Set if the whole pass was skipped (RECONCILE_STORAGE off) or errored.
    skipped: bool = False
    error: Optional[str] = None

    @property
    def consistent(self) -> bool:
        """True when DB, disk, and the library index all agree (nothing found)."""
        return not (self.dangling or self.orphaned_files or self.mis_sorted)

    def summary(self) -> str:
        """One-line human summary for the boot log."""
        if self.skipped:
            return "storage reconcile skipped"
        return (
            f"dangling={len(self.dangling)} "
            f"(flagged={len(self.flagged_missing)}, cleared={len(self.cleared_missing)}) "
            f"orphaned_files={len(self.orphaned_files)}"
            f"{' (capped)' if self.orphan_scan_capped else ''}"
            f"{' (skipped)' if self.orphan_scan_skipped else ''} "
            f"mis_sorted={len(self.mis_sorted)}"
        )


def expected_bucket(kind: str, image_type: Optional[str], video_type: Optional[str]) -> str:
    """The auto-sort bucket dir name for a library row's taxonomy.

    Mirrors the worker write paths so mis-sort detection matches reality:
      * film  → the literal ``film`` dir (app.film._film_output_path)
      * bild  → ``image_type`` or ``övrigt`` (app.render_worker, same fallback)
    """
    if kind == "film":
        return _FILM_BUCKET
    return image_type or _FALLBACK_BUCKET


def _expected_rel_path(row) -> Path:
    """The STORAGE_DIR-relative path this row's file *should* live at.

    ``output/<subject>/<area>/<bucket>/<filename>`` — bucket per
    :func:`expected_bucket`. The filename is taken from the row's stored
    ``file_path`` (we only re-derive the *directory*; the basename is whatever
    the render produced).
    """
    bucket = expected_bucket(row["kind"], row["image_type"], row["video_type"])
    filename = Path(row["file_path"]).name
    return Path("output") / row["subject"] / row["area"] / bucket / filename


def _reconcile_rows(storage_dir: Path, report: ReconcileReport) -> set[str]:
    """Dangling + mis-sort pass over live library rows. Returns known file_paths.

    One pass over the (small) library table:
      * absent file  → dangling; stamp ``missing_since`` if not already flagged.
      * present file that *was* flagged → clear the flag (self-heal).
      * present file in the wrong bucket → mis-sorted (report only).
    The returned set of (normalized) ``file_path`` strings is what the orphan
    walk diffs against, so the two passes share one DB read.
    """
    known: set[str] = set()
    flag_ids: list[str] = []
    clear_ids: list[str] = []

    with connect() as conn:
        rows = conn.execute(
            # Live rows only — a soft-deleted row (deleted_at set) is intentionally
            # gone from the library; its file may legitimately be absent.
            "SELECT id, kind, subject, area, image_type, video_type, file_path, "
            "missing_since FROM library WHERE deleted_at IS NULL"
        ).fetchall()

        for row in rows:
            rel = row["file_path"]
            if not rel:
                continue
            known.add(_norm(rel))
            abs_path = storage_dir / rel
            exists = abs_path.exists()

            if not exists:
                report.dangling.append(row["id"])
                if not row["missing_since"]:
                    flag_ids.append(row["id"])
                # else: already flagged on a prior boot → no re-write (idempotent).
                continue

            # File is present. Self-heal a stale flag.
            if row["missing_since"]:
                clear_ids.append(row["id"])

            # Present-but-mis-sorted: the file isn't under its taxonomy bucket.
            expected = _norm(str(_expected_rel_path(row)))
            if _norm(rel) != expected:
                report.mis_sorted.append(row["id"])

        # The ONLY writes in the whole reconcile: toggle the soft missing flag.
        # Batched, guarded individually so one bad id can't abort the rest.
        ts = now_iso()
        for jid in flag_ids:
            conn.execute(
                "UPDATE library SET missing_since = ? "
                "WHERE id = ? AND missing_since IS NULL AND deleted_at IS NULL",
                (ts, jid),
            )
        for jid in clear_ids:
            conn.execute(
                "UPDATE library SET missing_since = NULL WHERE id = ?", (jid,)
            )

    report.flagged_missing.extend(flag_ids)
    report.cleared_missing.extend(clear_ids)
    return known


def _scan_orphans(
    storage_dir: Path, known: set[str], report: ReconcileReport, max_scan: int
) -> None:
    """Walk ``output/`` for files with no library row. Bounded + report-only.

    Diffs each file's STORAGE_DIR-relative path against ``known`` (the file_paths
    the DB knows). Hard-capped at ``max_scan`` files: on hitting the cap we stop
    and set ``orphan_scan_capped`` rather than risk a long boot scan. Nothing is
    moved, indexed, or deleted — orphans are surfaced for the owner to decide.
    """
    output_dir = storage_dir / "output"
    if not output_dir.exists():
        return
    seen = 0
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        seen += 1
        if seen > max_scan:
            report.orphan_scan_capped = True
            log.warning(
                "reconcile: orphan scan hit cap (%d files); stopping early. "
                "Raise LARBILD_RECONCILE_MAX_SCAN or disable the scan if intended.",
                max_scan,
            )
            break
        try:
            rel = path.relative_to(storage_dir)
        except ValueError:  # pragma: no cover - rglob stays under storage_dir
            continue
        if _norm(str(rel)) not in known:
            report.orphaned_files.append(str(rel))


def _norm(rel: str) -> str:
    """Normalize a stored/derived relative path for comparison.

    The worker stores POSIX-style ``output/<subject>/...`` paths; normalize
    separators so a comparison is OS-independent and tolerant of a stray leading
    ``./``. We deliberately do NOT resolve symlinks or absolutize — the value is
    a logical key, matched as a string.
    """
    return Path(rel).as_posix().lstrip("./") or rel


def reconcile_storage(
    storage_dir: Union[str, Path],
    *,
    scan_orphans: Optional[bool] = None,
    max_scan: int = DEFAULT_MAX_SCAN,
) -> ReconcileReport:
    """Reconcile DB ↔ disk ↔ library and return a structured report (lb-qtr).

    Three-way consistency, conservatively: dangling rows are FLAGGED (soft
    ``missing_since``, never deleted) and un-flagged if the file reappears;
    orphaned files and mis-sorted assets are REPORTED only. Best-effort —
    catches and logs any error, returns a report with ``error`` set rather than
    raising, so a reconcile problem can never block boot.

    Args:
        storage_dir: the STORAGE_DIR root (``output/`` lives directly under it).
        scan_orphans: include the bounded orphan-file walk. Defaults to the
            ``LARBILD_RECONCILE_SCAN_ORPHANS`` env (on unless set falsey). Pass
            ``False`` to run only the DB-side dangling + mis-sort pass.
        max_scan: hard cap on files visited by the orphan walk (boot-time safety).

    Returns:
        A :class:`ReconcileReport` describing everything found + the (flag-only)
        writes made.
    """
    report = ReconcileReport()
    storage = Path(storage_dir)
    if scan_orphans is None:
        scan_orphans = _env_bool("LARBILD_RECONCILE_SCAN_ORPHANS", True)

    try:
        known = _reconcile_rows(storage, report)
        if scan_orphans:
            _scan_orphans(storage, known, report, max_scan)
        else:
            report.orphan_scan_skipped = True
    except Exception as e:  # noqa: BLE001 — reconcile must never block boot
        log.warning("reconcile_storage failed: %s", e)
        report.error = str(e)
        return report

    log.info("reconcile_storage: %s", report.summary())
    return report


def reconcile_storage_on_boot(app) -> ReconcileReport:
    """Wire :func:`reconcile_storage` into ``create_app`` after the job reconcile.

    Reads ``STORAGE_DIR`` from the Flask config and honours the
    ``RECONCILE_STORAGE`` config flag (default on; default OFF under TESTING so a
    plain ``create_app({'TESTING': True})`` unit test pays nothing). Best-effort:
    never raises — a reconcile issue must not stop the app from booting.
    """
    enabled = app.config.get(
        "RECONCILE_STORAGE", not app.config.get("TESTING", False)
    )
    if not enabled:
        return ReconcileReport(skipped=True)
    try:
        return reconcile_storage(app.config["STORAGE_DIR"])
    except Exception as e:  # noqa: BLE001 — defensive; reconcile_storage already guards
        log.warning("reconcile_storage_on_boot failed: %s", e)
        return ReconcileReport(skipped=True, error=str(e))


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var. Truthy = 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
