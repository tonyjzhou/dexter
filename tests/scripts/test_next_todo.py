"""Tests for scripts/next_todo.py — the deterministic TODOS.md backlog selector.

Regression focus: a `**Depends on:**` line whose clause the author marked SATISFIED
("none", "… is DONE") must bucket READY, not BLOCKED. Leaving a vestigial satisfied
Depends-on line on a ready P2 (instead of deleting it) hid that P2 from the `ready`
list, so the selector's stale P3 default kept re-surfacing and tripped the
loop_next_todo.sh spin-guard on an item no pass ever worked (the 2026-06-26 drain
halt, run-20260626T112516Z; root cause: L524 "Depends on: none (… is DONE)").
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_NEXT_TODO = Path(__file__).resolve().parents[2] / "scripts" / "next_todo.py"
_spec = importlib.util.spec_from_file_location("next_todo", _NEXT_TODO)
assert _spec and _spec.loader
next_todo = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass (under `from __future__ import annotations`)
# can resolve the module via sys.modules during class processing.
sys.modules["next_todo"] = next_todo
_spec.loader.exec_module(next_todo)


def _bucket_of(text: str, title_fragment: str) -> str:
    items, _closed = next_todo.parse(text)
    matches = [it for it in items if title_fragment in it.title]
    assert matches, f"no item titled containing {title_fragment!r}; got {[i.title for i in items]}"
    return matches[0].bucket


def _ready(text: str) -> list:
    """The `ready` bucket in selection order — exactly what main(--json) exposes and what
    loop_next_todo.sh's select_state() reads as the pick (`next` == ready[0])."""
    items, _closed = next_todo.parse(text)
    return next_todo._fmt(items, "ready")


def _next_pick(text: str):
    """The single item loop_next_todo.sh would work next: the head of the ready bucket
    (select_state() reads d['next'] == ready[0]). None when nothing is ready."""
    ready = _ready(text)
    return ready[0] if ready else None


def test_no_depends_line_is_ready() -> None:
    text = "### Plain item\n**Priority:** P2\n**What:** do a thing.\n"
    assert _bucket_of(text, "Plain item") == "ready"


def test_open_dependency_is_blocked() -> None:
    text = (
        "### Blocked item\n"
        "**Priority:** P2\n"
        "**Depends on:** labeled negative-equity calibration set (does not exist yet).\n"
    )
    assert _bucket_of(text, "Blocked item") == "blocked"


def test_depends_on_none_is_ready() -> None:
    """The L524 regression: `Depends on: none (… is DONE)` is a met dependency → READY."""
    text = (
        "### FISV governor item\n"
        "**Priority:** P2\n"
        "**Depends on:** none (the N=24 validation that surfaced it is DONE)\n"
        "**What:** redesign the governor.\n"
    )
    assert _bucket_of(text, "FISV governor item") == "ready"


def test_named_is_done_dependency_stays_blocked() -> None:
    """A NAMED 'X is DONE' dependency is NOT a mechanical promotion — verifying the named
    thing shipped is the /next-todo skill's Step-2 JUDGMENT, not the selector's. The
    selector must not infer satisfaction from a 'DONE' token in arbitrary prose."""
    text = (
        "### Named-dep item\n"
        "**Priority:** P1\n"
        "**Depends on:** the conviction-arm snapshot, which is DONE (v0.36.82.0).\n"
    )
    assert _bucket_of(text, "Named-dep item") == "blocked"


def test_compound_partially_done_dependency_stays_blocked() -> None:
    """The adversary-found regression: a compound clause where ONE sub-part carries
    '(done)' must NOT be wholesale-promoted while another sub-part is unmet (L442-class:
    'conviction gate ship-1 (done); SBC coverage states (B2 spine)')."""
    text = (
        "### Compound-dep item\n"
        "**Priority:** P2\n"
        "**Depends on:** conviction gate ship-1 (done); SBC coverage states (B2 spine).\n"
    )
    assert _bucket_of(text, "Compound-dep item") == "blocked"


def test_none_of_until_phrase_stays_blocked() -> None:
    """A clause that merely STARTS with 'none' but states a real condition
    ('none of … until X ships') is a genuine dependency, not 'no dependency'."""
    text = (
        "### None-of item\n"
        "**Priority:** P2\n"
        "**Depends on:** none of the candidates work until the calibration set ships.\n"
    )
    assert _bucket_of(text, "None-of item") == "blocked"


def test_depends_on_na_is_ready() -> None:
    text = "### NA-dep item\n**Priority:** P3\n**Depends on:** N/A — foundation already landed.\n"
    assert _bucket_of(text, "NA-dep item") == "ready"


def test_shelved_stays_parked_even_if_dependency_met() -> None:
    """A satisfied dependency must NOT override a deliberate SHELVED/parked marker."""
    text = (
        "### Parked item (SHELVED)\n"
        "**Priority:** P2\n"
        "**Depends on:** none — but deliberately shelved.\n"
    )
    assert _bucket_of(text, "Parked item") == "parked"


def test_closed_item_excluded() -> None:
    text = "### ~~Old item~~ — DONE (v1)\n**Priority:** P2\n**Depends on:** none.\n"
    items, closed = next_todo.parse(text)
    assert closed == 1
    assert not [it for it in items if "Old item" in it.title]


# --- Selection order: Readiness first, then Priority ---------------------------------
# The spec the loop selector must honor (request 2026-06-28): "select the next READY item
# based on Readiness and then Priority. If both P1 and P3 items are ready, the P1 item
# should be worked on first." The expected values below come from that SPEC and the
# priority convention (lower P-number = higher priority, DOMAIN.md), NOT from running the
# code — a spec-derived oracle. They pin the `next` pick that loop_next_todo.sh's
# select_state() reads as `d['next']` (== ready[0]); the existing tests above pin only
# bucket CLASSIFICATION, leaving the pick's ordering guarantee unprotected against drift.


def test_ready_p1_beats_ready_p3() -> None:
    """The literal request: with a READY P1 and a READY P3, the P1 is worked first.
    The P3 is placed FIRST in document order so a document-order-only selector would
    (wrongly) pick it — proving Priority, not text position, drives the pick."""
    text = (
        "### Ready P3 item\n"
        "**Priority:** P3\n"
        "**What:** lower priority, but appears first in the file.\n\n"
        "### Ready P1 item\n"
        "**Priority:** P1\n"
        "**What:** higher priority, appears later in the file.\n"
    )
    pick = _next_pick(text)
    assert pick is not None
    assert pick.priority == "P1"
    assert "Ready P1" in pick.title


def test_readiness_dominates_priority() -> None:
    """Readiness comes BEFORE Priority: a BLOCKED P0 (highest priority) must NOT be the
    pick while a READY P3 exists — the P3 is worked first because the P0 isn't ready."""
    text = (
        "### Blocked P0 item\n"
        "**Priority:** P0\n"
        "**Depends on:** a foundation that has not shipped yet.\n\n"
        "### Ready P3 item\n"
        "**Priority:** P3\n"
        "**What:** lower priority but actually ready.\n"
    )
    pick = _next_pick(text)
    assert pick is not None
    assert pick.priority == "P3", (
        "a BLOCKED higher-priority item must not be picked over a READY one"
    )
    assert _bucket_of(text, "Blocked P0") == "blocked"


def test_ready_bucket_is_priority_ordered() -> None:
    """The full ready ordering, scrambled in the file, must come out P0..P4 by priority
    (line number only breaks ties WITHIN a priority)."""
    text = (
        "### Ready P4 item\n**Priority:** P4\n**What:** x.\n\n"
        "### Ready P0 item\n**Priority:** P0\n**What:** x.\n\n"
        "### Ready P2 item\n**Priority:** P2\n**What:** x.\n\n"
        "### Ready P1 item\n**Priority:** P1\n**What:** x.\n"
    )
    assert [it.priority for it in _ready(text)] == ["P0", "P1", "P2", "P4"]


def test_same_priority_breaks_ties_by_document_order() -> None:
    """Among equal-priority READY items, the earlier-in-file one is picked first
    (the (rank, line) tie-break) — a stable, deterministic order."""
    text = (
        "### First P2 item\n**Priority:** P2\n**What:** appears first.\n\n"
        "### Second P2 item\n**Priority:** P2\n**What:** appears second.\n"
    )
    pick = _next_pick(text)
    assert pick is not None
    assert "First P2" in pick.title


# --- Headless-drain HAZARD flag (_HEADLESS_RISK) -------------------------------------
# The 11+th-occurrence guard (run-20260702T010353Z: the L590 full-fleet capex scan bg_yielded
# every pass and, as the #1-READY pick, stalled the whole drain). A READY item whose body reads
# as a long-running live-SEC/backfill op is flagged so the operator parks it BEFORE it stalls a
# loop. Oracle = the sweep-language definition + the actual incident phrases (`--all`, `data
# reextract`, "full seeded fleet", "live SEC", "~16k filings"), NOT the code — a spec-derived
# oracle. WARN-ONLY: the flag NEVER changes the bucket (asserted below).


def _item(text: str, title_fragment: str):
    items, _closed = next_todo.parse(text)
    matches = [it for it in items if title_fragment in it.title]
    assert matches, f"no item titled containing {title_fragment!r}"
    return matches[0]


def test_all_flag_marks_headless_risk_but_stays_ready() -> None:
    """A READY item invoking a `--all` full-fleet sweep is flagged yet stays READY —
    the flag is a nudge to park, not a control-flow change."""
    text = (
        "### Run the fleet scan\n"
        "**Priority:** P4\n"
        "**What:** run `scripts/scan_x.py --all` over the whole seeded fleet.\n"
    )
    it = _item(text, "Run the fleet scan")
    assert it.bucket == "ready"
    assert it.headless_risk, "a --all full-fleet sweep must be flagged headless-risk"


def test_live_sec_and_filing_count_phrasing_flagged() -> None:
    """The L590 phrasings — 'live SEC', '~16k filings', 'full seeded fleet' — each trip it."""
    for body in (
        "**What:** ~16k filings, slow, live SEC, bounded by --limit.",
        "**What:** re-parse across the whole seeded fleet (11,453 filings).",
        "**What:** a data reextract of every seeded company.",
    ):
        text = f"### Long op\n**Priority:** P4\n{body}\n"
        assert _item(text, "Long op").headless_risk, f"should flag: {body!r}"


def test_ordinary_code_item_not_flagged() -> None:
    """A normal code ticket must NOT be flagged — no false positives that cry wolf."""
    text = (
        "### Fix the CriteriaStrip N/A rendering\n"
        "**Priority:** P3\n"
        "**What:** forward company.filter_results verbatim so status=na renders neutral.\n"
    )
    assert _item(text, "CriteriaStrip").headless_risk == ""


def test_parked_sweep_item_stays_parked_and_is_not_warned() -> None:
    """A SHELVED full-fleet sweep stays PARKED (park wins); the human render warns only on
    READY rows, so a parked sweep does not emit the HEADLESS-RISK banner (it is already safe)."""
    text = (
        "### Full-fleet scan (SHELVED)\n"
        "**Priority:** P4 — SHELVED (operator-only)\n"
        "**What:** run the scan --all over the full seeded fleet, live SEC.\n"
    )
    it = _item(text, "Full-fleet scan")
    assert it.bucket == "parked"
    rendered = next_todo.render_human(*next_todo.parse(text), top=None)
    assert "HEADLESS-RISK" not in rendered


def test_flagged_top_pick_emits_warning_in_human_render() -> None:
    """When the #1 READY pick is a headless-risk sweep, the operator-facing render surfaces
    the warning — the exact case that stalls loop_next_todo.sh."""
    text = "### Fleet sweep\n**Priority:** P4\n**What:** scan --all the whole seeded fleet.\n"
    items, closed = next_todo.parse(text)
    rendered = next_todo.render_human(items, closed, top=1)
    assert "HEADLESS-RISK" in rendered
