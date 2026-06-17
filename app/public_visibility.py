"""Public-gallery visibility predicate (crowdsource publish-all).

The internet-facing read-only instance (bilder.sibbamala.com, the
``LARBILD_PUBLIC_READONLY`` flag) historically showed ONLY owner-approved
images (``review_ok = 1``). The owner now wants to *crowdsource* the discovery
of broken/wrong images in three subjects — so on the PUBLIC instance those
subjects also show UN-reviewed images (``review_ok IS NULL``), giving "more
eyes" on material the owner has not vetted yet. Rejected images
(``review_ok = 0``) stay hidden everywhere, and every OTHER subject stays
approved-only.

This module is the single source of truth for that rule so the three places
that gate public visibility — the gallery item list, the per-node counts, and
the ``/media`` byte server — can never drift on the definition of "visible".

Definitions (PUBLIC instance only; the private :6868 tool is unaffected and
shows everything regardless of ``review_ok``):

* ``review_ok`` is the tri-state verdict: ``1`` = approved, ``0`` = rejected,
  ``NULL`` = un-reviewed (pending).
* For a CROWDSOURCE subject → visible when ``review_ok IS NULL OR review_ok = 1``
  (approved OR pending; rejected hidden).
* For any other subject → visible when ``review_ok = 1`` (approved only).

SFW-only: pure visibility plumbing over the existing SFW library; no restricted
column, content, or feature.
"""
from __future__ import annotations

import os

__all__ = ["CROWDSOURCE_SUBJECTS", "public_readonly_from_env", "visible_predicate_sql"]

# The subjects whose UN-reviewed images are published on the PUBLIC instance for
# crowdsourced error-spotting. Taxonomy slugs (app.taxonomy.SUBJECTS): note the
# real slug for "matte" is ``matematik``. Kept as a frozenset so it is an
# immutable, membership-cheap source of truth shared by every gate below.
CROWDSOURCE_SUBJECTS: frozenset[str] = frozenset({"fysik", "matematik", "teknik"})

# The two stored predicates. ``review_ok`` is INTEGER CHECK (review_ok IN (0,1))
# and NULLABLE. Approved-only = the historical public rule; publish-all =
# approved OR pending (NEVER rejected).
_APPROVED_ONLY_SQL = "review_ok = 1"
_PUBLISH_ALL_SQL = "(review_ok IS NULL OR review_ok = 1)"


def public_readonly_from_env() -> bool:
    """True if ``LARBILD_PUBLIC_READONLY`` is set truthy in the environment.

    The same switch the factory + the per-route import-time guards read. Callers
    capture it ONCE at import (matching the route modules) and pass the captured
    value to :func:`visible_predicate_sql`, so the predicate can never disagree
    with the caller's own public/private wiring.
    """
    return os.environ.get("LARBILD_PUBLIC_READONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def visible_predicate_sql(public_readonly: bool, subject_col: str = "subject") -> str:
    """The SQL predicate selecting publicly-VISIBLE rows, as a string fragment.

    Returns a clause to AND into a library query, given the CALLER's
    ``public_readonly`` flag (so this never re-reads the environment and can never
    diverge from the caller's import-time wiring):

      * NOT public → the historical approved-only clause (``review_ok = 1``)
        verbatim, so a private/test caller that AND-s it in is unchanged.
      * public → per-row: a row whose subject is in :data:`CROWDSOURCE_SUBJECTS`
        is visible when approved-or-pending (``review_ok IS NULL OR review_ok =
        1``); any other row only when approved (``review_ok = 1``). Rejected
        (``review_ok = 0``) is never visible.

    ``subject_col`` is the column name to test (default ``subject``); a caller
    that aliases the table can pass e.g. ``"l.subject"``. The crowdsource subject
    list is inlined as quoted string literals — a fixed, code-controlled allowlist
    (never user input), so there is no injection surface and the clause needs no
    bound parameters (drop-in for the existing f-string SQL).
    """
    if not public_readonly:
        return _APPROVED_ONLY_SQL
    # Quote the fixed allowlist as SQL string literals (no user input here).
    quoted = ", ".join(f"'{s}'" for s in sorted(CROWDSOURCE_SUBJECTS))
    # Per-row: crowdsource subjects → publish-all; everything else → approved-only.
    return (
        f"(CASE WHEN {subject_col} IN ({quoted}) "
        f"THEN {_PUBLISH_ALL_SQL} ELSE {_APPROVED_ONLY_SQL} END)"
    )
