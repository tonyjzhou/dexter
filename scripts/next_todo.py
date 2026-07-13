#!/usr/bin/env python3
"""next_todo.py — deterministic candidate selector for TODOS.md.

The /next-todo skill's eyes. Parses TODOS.md and ranks the active backlog so the
agent picks the next item by *dependency-aware priority* instead of eyeballing a
1200-line file (error-prone + token-heavy). Read-only; mutates nothing.

It does the MECHANICAL half (extract every priority-tagged item, drop the closed
ones, split into ready / blocked / parked); the skill does the JUDGMENT half
(is a blocked item's `Depends on:` actually satisfied now — e.g. its dependency
shipped — and which track does the pick route to).

Buckets:
  READY   — no `Depends on:` line, OR a `Depends on:` clause that is just "none" / "n/a"
            (the author asserting no real dependency). The default pick is the top of this list.
  BLOCKED — an UNMET `Depends on:` line — including a NAMED dependency ("X is DONE") or a
            compound clause, where confirming it is met is JUDGMENT the skill does, not the
            selector. Shown with the dependency text so the skill can promote a met one.
  PARKED  — trigger-gated / SHELVED. Never auto-picked (a deliberate non-decision).
Closed items (struck `~~…~~`, `— DONE`, `**Completed:**`, `→ done`, `RESOLVED`)
are excluded entirely.

Convention this relies on (stable in TODOS.md):
  * an item title is a `### ` heading OR a full-line `**bold**` sub-heading;
  * its priority is the first `**Priority:** P0..P4` line in its body;
  * a hard dependency is written `**Depends on:** …` (foundations that *enable*
    others say "prerequisite", which is deliberately NOT matched — they are READY).

Usage:
  python3 scripts/next_todo.py            # human-readable buckets (run from repo root)
  python3 scripts/next_todo.py --json     # machine-readable, for the skill to parse
  python3 scripts/next_todo.py --top 1    # just the single next pick (READY head)
  python3 scripts/next_todo.py --file path/to/TODOS.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
_PRIORITY_RE = re.compile(r"^\*\*Priority:\*\*\s*(P[0-4]|RESOLVED)\b")
_H2_RE = re.compile(r"^##\s+(.*\S)\s*$")
_H3_RE = re.compile(r"^###\s+(.*\S)\s*$")
# a full-line bold span used as a sub-item title, e.g. **Gate-2 distribution CLI flag**
# (excludes labelled bold lines like **Why:** / **Priority:** which carry trailing prose)
_BOLD_TITLE_RE = re.compile(r"^\*\*(?!\w[\w /-]*:\*\*)([^*][^*]*?)\*\*\s*$")
_DEPENDS_RE = re.compile(
    r"\*\*Depends on(?:\s*/\s*coordinate(?:\s*with)?)?:\*\*\s*(.+)", re.IGNORECASE
)
# A `Depends on:` clause the author has marked NO real dependency — the clause is just
# "none"/"n/a" (optionally followed by a parenthetical or dash/colon explanation, e.g.
# "none (… is DONE)"). This is mechanical and unambiguous, so the item is READY. A NAMED
# dependency ("X is DONE", or a compound "A (done); B (open)") is deliberately NOT matched:
# verifying a named thing actually shipped is JUDGMENT, which /next-todo's Step-2 does by
# reading the dependency — the selector must NOT infer it from a coincidental "(done)"
# token, which would false-promote a still-blocked compound clause. "none of … until X"
# (a clause that only starts with "none") also stays blocked.
_DEP_SATISFIED_RE = re.compile(
    r"^\s*(?:none|n/?a)\b\s*(?:[-—(.:,]|$)",
    re.IGNORECASE,
)

# Closed → exclude from the backlog entirely.
_CLOSED_TITLE = re.compile(r"~~|—\s*DONE\b", re.IGNORECASE)
_CLOSED_PRIORITY = re.compile(r"→\s*done|mostly done|\bRESOLVED\b", re.IGNORECASE)
# Parked → show but never auto-pick.
_PARKED = re.compile(r"trigger-gated|\bSHELVED\b", re.IGNORECASE)

# --- park (WRITE side of the PARKED bucket) --------------------------------
# The `park` subcommand is the deterministic auto-park backstop's hand: it appends a
# `trigger-gated` marker to an item's **Priority:** line so THIS SAME `_PARKED` regex re-buckets
# it PARKED — writer and reader single-sourced, so they cannot drift. Verify-then-write: the edit
# is applied in memory, re-parsed, and committed to disk ONLY if the item now buckets 'parked'
# (never 'closed'), so a stray token in the reason can never silently drop an item from the backlog.
_PARK_REASON_MAX = 100
_PARK_MARKER = "(trigger-gated: awaiting design decision — {reason})"
# Tokens that, if they leaked into the appended marker, would make parse() mis-bucket the item as
# CLOSED (_CLOSED_PRIORITY on the Priority line) instead of PARKED, or perturb the line's parsing —
# stripped from the reason before it is embedded (the verify-then-write below is the backstop).
_PARK_REASON_STRIP = re.compile(r"~~|\*\*|→\s*done|mostly done|\bRESOLVED\b|[()]", re.IGNORECASE)

# Headless-drain HAZARD: a READY item whose body reads like a LONG-RUNNING live-SEC /
# full-fleet / backfill operation cannot finish in a one-shot `claude -p` turn, so a
# /next-todo pass on it bg_yields — the agent backgrounds the op and ends its turn awaiting
# a completion callback headless never delivers, stranding the whole drain if the item ranks
# #1-READY (loop_next_todo.sh; project_loop_next_todo_no_land_root_causes, 11+ occurrences:
# `data reextract`, scan_capex `--all`, the L590 full-fleet capex scan). This flags such an
# item so the operator PARKS it (SHELVED) or runs it synchronously BEFORE it halts a loop.
# WARN-ONLY, never auto-parks: the loop's design is deliberately stop-and-ask, and `--all` /
# "backfill" also appear in benign prose, so a false match must never silently hide real work.
_HEADLESS_RISK = re.compile(
    r"(?<![\w-])--all\b"  # full-fleet CLI flag
    r"|\bre-?extract\b"  # `data reextract` (idle-timeout op)
    r"|\bdata\s+(?:reextract|backfill)\b"  # long-running data CLI ops
    r"|\b(?:full|whole|entire)\s+seeded\s+fleet\b"  # the L590 phrasing
    r"|\bover\s+the\s+(?:full|whole|entire)\b[^.\n]{0,40}\bfleet\b"
    r"|\blive\s+SEC\b"  # explicit live-SEC sweep
    r"|\b\d[\d,]{3,}\s+filings\b"  # e.g. "11,453 filings" / "11453 filings"
    r"|~?\s*\d+\s*k\s+filings\b",  # e.g. "~16k filings"
    re.IGNORECASE,
)


@dataclass
class Item:
    title: str
    section: str
    priority: str
    line: int
    bucket: str  # ready | blocked | parked
    depends_on: str = ""
    headless_risk: str = ""  # matched sweep-phrase if the body reads as a long-running live op

    @property
    def rank(self) -> int:
        return PRIORITY_RANK.get(self.priority, 9)


def _is_title(line: str) -> str | None:
    """Return the title text if this line opens an item, else None."""
    m = _H3_RE.match(line)
    if m:
        return m.group(1)
    m = _BOLD_TITLE_RE.match(line)
    if m:
        return m.group(1)
    return None


def parse(text: str) -> tuple[list[Item], int]:
    """Return (active_items, closed_count). Active = ready | blocked | parked."""
    lines = text.splitlines()
    # Index every title and section boundary so an item's body is title..next-boundary.
    section = ""
    titles: list[tuple[int, str, str]] = []  # (idx, title, section_at_that_point)
    for i, ln in enumerate(lines):
        m = _H2_RE.match(ln)
        if m:
            section = m.group(1)
            continue
        t = _is_title(ln)
        if t is not None:
            titles.append((i, t, section))

    # Boundary set: any title OR any ## line ends the previous item's body.
    section_idxs = [i for i, ln in enumerate(lines) if _H2_RE.match(ln)]
    title_idxs = [i for i, _, _ in titles]
    boundaries = sorted(set(title_idxs) | set(section_idxs))

    def body_end(start: int) -> int:
        for b in boundaries:
            if b > start:
                return b
        return len(lines)

    items: list[Item] = []
    closed = 0
    for idx, title, sect in titles:
        end = body_end(idx)
        body = lines[idx + 1 : end]

        priority = ""
        for bl in body:
            pm = _PRIORITY_RE.match(bl)
            if pm:
                priority = pm.group(1)
                priority_line = bl
                break
        if not priority:
            continue  # a grouping/prose heading, not a tracked item

        # Closed?
        if _CLOSED_TITLE.search(title) or _CLOSED_PRIORITY.search(priority_line):
            closed += 1
            continue
        if any(bl.startswith("**Completed:**") for bl in body):
            closed += 1
            continue

        depends = ""
        dep_satisfied = False
        for bl in body:
            dm = _DEPENDS_RE.search(bl)
            if dm:
                # trim to the dependency clause (first sentence / up to a bold marker)
                clause = dm.group(1).split("**")[0].strip()
                clause = re.split(r"(?<=[a-z])\.\s", clause)[0].strip().rstrip(".")
                depends = clause[:90]
                # A dependency the author has marked MET must not pin the item in BLOCKED.
                # Leaving a satisfied `Depends on:` line (instead of deleting it) was hiding
                # a ready P2 from the selector, so the stale P3 default kept re-surfacing and
                # tripped the loop spin-guard on an item no pass ever worked.
                dep_satisfied = bool(_DEP_SATISFIED_RE.search(clause))
                break

        if _PARKED.search(title) or _PARKED.search(priority_line):
            bucket = "parked"
        elif depends and not dep_satisfied:
            bucket = "blocked"
        else:
            bucket = "ready"

        # Flag a long-running live-SEC/backfill op (see _HEADLESS_RISK). Scanned over the
        # title + full body so the phrasing is caught wherever the author wrote it; surfaced
        # as a warning only on READY items (a parked one is already safe; a blocked one gets
        # re-checked once it becomes ready), never used to change the bucket.
        risk_m = _HEADLESS_RISK.search(title + "\n" + "\n".join(body))
        headless_risk = risk_m.group(0).strip() if risk_m else ""

        items.append(
            Item(
                title=title,
                section=sect,
                priority=priority,
                line=idx + 1,
                bucket=bucket,
                depends_on=depends,
                headless_risk=headless_risk,
            )
        )

    items.sort(key=lambda it: (it.rank, it.line))
    return items, closed


def _fmt(items: list[Item], bucket: str) -> list[Item]:
    return [it for it in items if it.bucket == bucket]


def render_human(items: list[Item], closed: int, top: int | None) -> str:
    ready = _fmt(items, "ready")
    blocked = _fmt(items, "blocked")
    parked = _fmt(items, "parked")
    out: list[str] = []
    out.append(f"TODOS.md — {len(items)} active items · {closed} closed\n")

    def block(
        label: str, rows: list[Item], show_dep: bool = False, warn_risk: bool = False
    ) -> None:
        out.append(label)
        if not rows:
            out.append("  (none)")
        for it in rows:
            out.append(f"  {it.priority}  L{it.line:<5} {it.title}")
            out.append(f"           § {it.section}")
            if show_dep and it.depends_on:
                out.append(f"           → depends on: {it.depends_on}")
            if warn_risk and it.headless_risk:
                out.append(
                    f'           ⚠ HEADLESS-RISK ("{it.headless_risk}"): reads as a long-running '
                    "live-SEC/backfill op — a /next-todo pass will bg_yield and stall the loop. "
                    "PARK it (add SHELVED to **Priority:**) or run it synchronously yourself."
                )
        out.append("")

    if top:
        block(f">> NEXT PICK (top {top} ready, dependency-free):", ready[:top], warn_risk=True)
        return "\n".join(out)

    block(">> NEXT READY  (no 'Depends on' — pick the top one):", ready, warn_risk=True)
    block("-- BLOCKED   (verify the dependency is met before picking):", blocked, show_dep=True)
    block("~~ PARKED    (trigger-gated / shelved — do not auto-pick):", parked)
    return "\n".join(out)


def _sanitize_park_reason(reason: str) -> str:
    """One-line, closed-marker-free, length-capped reason for the park marker."""
    r = _PARK_REASON_STRIP.sub(" ", reason or "")
    r = " ".join(r.split())  # collapse all whitespace/newlines to single spaces
    r = r.strip(" -—:")
    if len(r) > _PARK_REASON_MAX:
        r = r[: _PARK_REASON_MAX - 1].rstrip() + "…"
    return r or "awaiting an operator design decision"


def _priority_line_idx(lines: list[str], heading_idx: int) -> int | None:
    """Index (in `lines`) of the item's first **Priority:** line, or None if the item has none
    before its body ends (the next title / ## section). Mirrors parse()'s body-boundary logic so
    a later item's Priority line is never grabbed for a header that has none."""
    for j in range(heading_idx + 1, len(lines)):
        ln = lines[j]
        if _is_title(ln) or _H2_RE.match(ln):
            return None
        if _PRIORITY_RE.match(ln):
            return j
    return None


def park_item(path: Path, line: int, reason: str) -> int:
    """Auto-park the tracked item whose heading is at 1-based `line`: append the trigger-gated
    marker to its **Priority:** line so next_todo re-buckets it PARKED. VERIFY-then-write — the
    edit is re-parsed in memory and written ONLY if the item now buckets 'parked' (never 'closed'
    or still 'ready'), so a bad reason can't silently close or fail to park an item. Idempotent on
    an already-parked item. Preserves every other byte (edits one line in place).

    Returns 0 on success; non-zero (nothing written) on any failure so the caller falls back to the
    manual stop: 2 file missing · 3 line out of range · 4 no tracked item at that line · 5 no
    **Priority:** line · 6 marker did not re-bucket PARKED."""
    if not path.exists():
        return 2
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    idx = line - 1
    if idx < 0 or idx >= len(lines):
        return 3
    item = next((it for it in parse(text)[0] if it.line == line), None)
    if item is None:
        return 4  # heading at `line` isn't a priority-bearing (tracked) item, or it's closed
    if item.bucket == "parked":
        return 0  # already parked — idempotent no-op success
    pj = _priority_line_idx(lines, idx)
    if pj is None:
        return 5
    marker = _PARK_MARKER.format(reason=_sanitize_park_reason(reason))
    raw = lines[pj]
    content = raw.rstrip("\r\n")
    eol = raw[len(content) :]  # "\n" / "\r\n" / "" (last line, no trailing newline)
    new_lines = lines[:]
    new_lines[pj] = f"{content} {marker}{eol}"
    new_text = "".join(new_lines)
    parked = next((it for it in parse(new_text)[0] if it.line == line), None)
    if parked is None or parked.bucket != "parked":
        return 6  # would have mis-classified (closed / still ready) — refuse to write
    path.write_text(new_text, encoding="utf-8")
    return 0


def cmd_park(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="next_todo.py park",
        description="Append a trigger-gated marker to an item's **Priority:** line (auto-park).",
    )
    ap.add_argument("--file", default="TODOS.md", help="path to the backlog (default: TODOS.md)")
    ap.add_argument("--line", type=int, required=True, help="1-based heading line of the item")
    ap.add_argument("--reason", default="", help="one-line context for the parked marker")
    args = ap.parse_args(argv)
    rc = park_item(Path(args.file), args.line, args.reason)
    if rc != 0:
        print(
            f"next_todo park: could not park the item at line {args.line} (rc={rc})",
            file=sys.stderr,
        )
    return rc


def main(argv: list[str] | None = None) -> int:
    # `park` is a WRITE subcommand (the auto-park backstop's hand); everything else is the
    # read-only selector. Dispatch park before the flat selector arg-parse so the existing
    # `next_todo.py [--json|--top|--file]` interface every caller uses stays byte-for-byte.
    _args = sys.argv[1:] if argv is None else argv
    if _args and _args[0] == "park":
        return cmd_park(_args[1:])
    ap = argparse.ArgumentParser(
        description="Rank the active TODOS.md backlog by dependency-aware priority."
    )
    ap.add_argument(
        "--file", default="TODOS.md", help="path to the backlog (default: TODOS.md at repo root)"
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--top", type=int, default=None, help="print only the top N ready picks")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        print(f"error: {path} not found (run from repo root)", file=sys.stderr)
        return 2

    items, closed = parse(path.read_text(encoding="utf-8"))

    if args.json:
        ready = _fmt(items, "ready")
        payload = {
            "closed": closed,
            "next": asdict(ready[0]) if ready else None,
            "ready": [asdict(it) for it in ready],
            "blocked": [asdict(it) for it in _fmt(items, "blocked")],
            "parked": [asdict(it) for it in _fmt(items, "parked")],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(render_human(items, closed, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
