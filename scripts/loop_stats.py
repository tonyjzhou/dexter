#!/usr/bin/env python3
"""Per-pass + end-of-run statistics for scripts/loop_next_todo.sh.

The bash loop owns orchestration; this module owns all stats parsing and
formatting so the fiddly bits (agent headless JSON result schemas, token/cost
aggregation, the summary table) are unit-testable instead of buried in bash
string-mangling. Mirrors the next_todo.py split: bash calls a small,
deterministic python tool and renders its stdout.

Agents (LOOP_AGENT):
  * claude — `claude -p --output-format json|stream-json` result schema
  * grok   — `grok -p --output-format json|streaming-json` result schema

Both normalize into the SAME persisted blob under the historical JSONL key
`claude` (stable across run history; treat it as "agent stats"). The optional
top-level `agent` field records which CLI produced the pass.

Two subcommands:

  record   Parse ONE pass's captured agent output, append a normalized
           JSON record to the run's JSONL file, and print a compact one-line
           console summary for that pass (the "ongoing read").

  summary  Read the whole run JSONL and print the aggregate block + a
           per-pass table (the "summary after all items finished"). Printed on
           every loop exit path, so it also works for a partial/interrupted run.

The record schema is intentionally stable and self-describing — `summary` only
ever reads fields it wrote, and tolerates older records missing newer keys.

stdlib only (no jq, no third-party deps) — same contract as next_todo.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

# Cap on the persisted `terminal_excerpt` (blocked/stalled passes only — see _norm_claude). Large
# enough to quote the actual fork, small enough that a multi-pass run's JSONL stays tens-of-KB, not
# MB (project_loop_next_todo_no_land_root_causes, 10th occurrence).
_TERMINAL_EXCERPT_MAX_CHARS = 2000

# Cap on the one-line `park-summary` reason handed to next_todo's park marker. Short enough for a
# readable TODOS **Priority:** line; next_todo re-sanitizes + caps to its own limit downstream.
_PARK_SUMMARY_MAX = 140

# --- agent headless result parsing (claude + grok → one normalized blob) -----


def _try_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _read_result_text(path: str | None) -> str | None:
    """Read a captured agent stdout file fail-soft (None on missing/empty/IO)."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return txt or None


def parse_claude_result(path: str | None) -> dict | None:
    """Extract the terminal `type:result` object from captured claude output.

    Handles three shapes, fail-soft (returns None, never raises):
      * `--output-format json`        — the whole file is one result object.
      * `--output-format stream-json` — newline-delimited events; the LAST
                                        `type:result` line is the summary.
      * partial / crashed / empty     — no usable result; caller logs rc only.
    """
    txt = _read_result_text(path)
    if not txt:
        return None

    whole = _try_json(txt)
    if isinstance(whole, dict) and (whole.get("type") == "result" or "result" in whole):
        return whole

    found: dict | None = None
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = _try_json(line)
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj  # keep the last one
    return found


def parse_grok_result(path: str | None) -> dict | None:
    """Extract a terminal result object from captured `grok -p` output.

    Handles three shapes, fail-soft (returns None, never raises):
      * `--output-format json`           — one object with `text` / `stopReason`
                                           (or `type:error` + `message`).
      * `--output-format streaming-json` — NDJSON events; text chunks + final
                                           `type:end` / `type:error`.
      * partial / crashed / empty        — no usable result; caller logs rc only.

    Returns a dict shaped for `_norm_grok` (always has `text` when parsed from
    streaming, or the raw error object).
    """
    txt = _read_result_text(path)
    if not txt:
        return None

    whole = _try_json(txt)
    # Final JSON blob (success has `text`; error has type:error).
    if isinstance(whole, dict) and (
        "text" in whole or whole.get("type") == "error" or "stopReason" in whole
    ):
        return whole

    # streaming-json: accumulate text chunks; keep last end/error metadata.
    parts: list[str] = []
    meta: dict | None = None
    saw_event = False
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = _try_json(line)
        if not isinstance(obj, dict) or "type" not in obj:
            continue
        saw_event = True
        etype = obj.get("type")
        if etype == "text":
            data = obj.get("data")
            if isinstance(data, str):
                parts.append(data)
        elif etype == "thought":
            continue
        elif etype in ("end", "error", "max_turns_reached"):
            meta = obj
    if not saw_event:
        return None
    if meta is None and not parts:
        return None
    out: dict = dict(meta) if meta is not None else {"type": "end", "stopReason": "EndTurn"}
    # Prefer assembled stream text; fall back to any text field on the end event.
    if parts:
        out["text"] = "".join(parts)
    elif not isinstance(out.get("text"), str) and isinstance(out.get("message"), str):
        out["text"] = out["message"]
    return out


def _snippet_and_terminal(raw_result: str | None) -> dict:
    """Shared snippet + terminal_signal/excerpt derivation for any agent text.

    Classify TERMINAL INTENT (bg-yield vs decision-fork) from the FULL result
    text BEFORE truncating — markers routinely fall after a long preamble
    (project_loop_next_todo_no_land_root_causes, 10th occurrence).
    """
    terminal_signal = _classify_terminal_signal(raw_result)
    if isinstance(raw_result, str):
        snippet = raw_result.strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:157] + "…"
    else:
        snippet = None
    terminal_excerpt = None
    if terminal_signal is not None and isinstance(raw_result, str):
        terminal_excerpt = raw_result.strip().replace("\n", " ")
        if len(terminal_excerpt) > _TERMINAL_EXCERPT_MAX_CHARS:
            terminal_excerpt = terminal_excerpt[: _TERMINAL_EXCERPT_MAX_CHARS - 1] + "…"
    return {
        "result_snippet": snippet,
        "terminal_signal": terminal_signal,
        "terminal_excerpt": terminal_excerpt,
    }


def _norm_claude(result: dict | None) -> dict:
    """Reduce a raw claude result object to the fields we persist."""
    if not isinstance(result, dict):
        return {"parsed": False}
    usage = result.get("usage")
    if not isinstance(usage, dict):
        # Fail soft on a truthy-but-malformed usage (list/str/number from a
        # partial/corrupt result) — mirrors the permission_denials/result guards
        # below; an `or {}` would let a non-empty list through and AttributeError.
        usage = {}
    denials = result.get("permission_denials")
    raw_result = result.get("result")
    out = {
        "parsed": True,
        "subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "num_turns": result.get("num_turns"),
        "cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "tokens": {
            "input": usage.get("input_tokens") or 0,
            "output": usage.get("output_tokens") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "cache_creation": usage.get("cache_creation_input_tokens") or 0,
        },
        "permission_denials": len(denials) if isinstance(denials, list) else 0,
    }
    out.update(_snippet_and_terminal(raw_result if isinstance(raw_result, str) else None))
    return out


def _norm_grok(result: dict | None) -> dict:
    """Reduce a raw grok result object to the SAME fields `_norm_claude` persists.

    Grok's headless JSON does not currently expose cost/tokens/turns the way
    Claude's does — those fields stay None/0. Terminal-intent classification
    still runs on the full answer text so spin-guard arms (blocked/stalled) work.
    """
    if not isinstance(result, dict):
        return {"parsed": False}
    is_error_event = result.get("type") == "error"
    raw_result = result.get("text")
    if not isinstance(raw_result, str):
        msg = result.get("message")
        raw_result = msg if isinstance(msg, str) else None
    stop = result.get("stopReason")
    subtype: str | None
    if is_error_event:
        subtype = "error"
    elif isinstance(stop, str) and stop:
        subtype = stop
    else:
        subtype = "success"
    # Treat explicit error events / Error stop reasons as hard errors so the
    # loop's ERROR-retry arm fires the same way as Claude is_error=true.
    is_error = is_error_event or (
        isinstance(stop, str) and stop.lower() in ("error", "failed", "failure")
    )
    out = {
        "parsed": True,
        "subtype": subtype,
        "is_error": is_error,
        "num_turns": result.get("num_turns"),  # usually absent
        "cost_usd": result.get("total_cost_usd") or result.get("cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "duration_api_ms": result.get("duration_api_ms"),
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        },
        "permission_denials": 0,
    }
    usage = result.get("usage")
    if isinstance(usage, dict):
        out["tokens"] = {
            "input": usage.get("input_tokens") or usage.get("input") or 0,
            "output": usage.get("output_tokens") or usage.get("output") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "cache_creation": usage.get("cache_creation_input_tokens") or 0,
        }
    out.update(_snippet_and_terminal(raw_result))
    return out


def normalize_agent_result(path: str | None, agent: str = "claude") -> dict:
    """Parse + normalize one pass's captured agent stdout into the stable blob.

    `agent` is `claude` (default) or `grok`. Unknown values fall through to the
    claude parser (fail-soft for a typo'd flag rather than dropping the record).
    """
    agent_l = (agent or "claude").strip().lower()
    if agent_l == "grok":
        return _norm_grok(parse_grok_result(path))
    return _norm_claude(parse_claude_result(path))


# --- formatting helpers ------------------------------------------------------


def fmt_dur(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 0:
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    return f"{m}m{sec:02d}s"


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_cost(usd: float | None) -> str:
    if usd is None:
        return "$—"
    return f"${usd:,.2f}"


def _truncate(text: str, width: int) -> str:
    text = text or ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


# Final-result phrases that read as a mid-task BACKGROUND-JOB YIELD: the agent ended its turn
# expecting to be re-invoked when a `run_in_background` job finishes — a callback headless
# `claude -p` never delivers, so the turn just ends and the work is lost. Curated high-signal set
# (project_loop_next_todo_no_land_root_causes, 6th occurrence 2026-06-24: the IFRS value-fidelity
# probe yielded during INVESTIGATION with "…I'll await its completion notification rather than
# poll", leaving a CLEAN tree). Matched case-insensitively as substrings; only consulted on an
# already-"no progress" clean-tree no-land, so a false match merely relabels (never control flow).
_BG_YIELD_MARKERS: tuple[str, ...] = (
    "completion notification",
    "rather than poll",
    "await its completion",
    "await the completion",
    "awaiting its completion",
    "awaiting completion",
    "i'll await",
    "i will await",
    "i'll wait for",
    "i will wait for",
    "wait for it to complete",
    "wait for it to finish",
    "once it completes",
    "once it finishes",
    "when it completes",
    "when it finishes",
    "running in the background",
    "in the background",
)


def _looks_like_bg_yield(snippet: str | None) -> bool:
    """Heuristic: does a pass's final result text read as a mid-task background-job YIELD?

    Best-effort LABEL ENRICHMENT only. It splits an already-"no progress" clean-tree no-land into
    `stalled` (the result reads as an abandoned-mid-task yield — re-run is safe, no work to recover)
    vs `no-land` (a genuine stop: a blocked item, a deliberate no-op). A miss in either direction
    just changes one word — never control flow, never the abandoned-mid-land work-recovery path
    (that keys on a DIRTY tree, which this branch never sees)."""
    if not isinstance(snippet, str):
        return False
    s = snippet.lower()
    return any(m in s for m in _BG_YIELD_MARKERS)


# A clean-tree no-land where the agent hit a genuine DECISION FORK it cannot resolve headless and
# ESCALATED — it tried AskUserQuestion (unavailable under `claude -p`), then surfaced the fork in
# prose instead of guessing. This is NOT stuck and NOT a bg-yield: the agent correctly refused to
# build on a debatable premise and asked for a human call. Curated from the real L1382 pass
# (project_loop_next_todo_no_land_root_causes, 8th occurrence 2026-06-25: "`AskUserQuestion` isn't
# available here, so I'll surface this in prose. This is a genuine fork worth your call before I
# build…"). High-signal substrings only — matched case-insensitively and consulted ONLY on an
# already-"no progress" clean-tree no-land that is not a bg-yield, so a false match merely relabels
# one word (never control flow, never the work-recovery path, which keys on a DIRTY tree this never
# sees). Labeled `blocked` so the summary tells the operator to GO DECIDE (then park or build).
_DECISION_FORK_MARKERS: tuple[str, ...] = (
    "askuserquestion",
    "surface this in prose",
    "surface it in prose",
    "worth your call",
    "your call before i build",
    "genuine fork",
    "a fork worth your",
    "needs your decision",
    "needs a human decision",
    "this is a decision for you",
    # `/next-todo` Track-A vocabulary: the skill routes a "new subsystem" pick to a
    # SHAPE-FIRST design fork ("Headless Track-A decision-fork → park-and-continue").
    # When a pass mis-escalates that fork instead of parking (run-20260630T123905Z L2125
    # "it's a genuine Track A fork, so I'm pausing here"), "genuine fork" does NOT
    # substring-match "genuine Track A fork", so without these the pass mislabelled as a
    # generic `no-land` and the operator got "inspect, then re-run" (a re-run spin-stops
    # the same way) instead of the `blocked` "GO DECIDE, then park" guidance.
    # (project_loop_next_todo_no_land_root_causes, Track-A-fork occurrence 2026-06-30.)
    "track a fork",
    "track-a fork",
    "shape first",
    "shape it first",
    # `/next-todo` Track-A SHAPE-FIRST escalation that PRESENTS OPTIONS instead of building: a
    # mis-escalated Track-A pass often ends by laying the design choices out for a human to pick
    # — e.g. "Research done. Here's the shape of the codebase … then two concrete build options" —
    # never uttering any AskUserQuestion / "genuine fork" / "Track A fork" wording above. Keying on
    # the options-presentation phrasing catches that shape (run-20260630T204420Z-25533 pass [8],
    # L2904 "Quality-first discovery lane" — a Track-A new subsystem picked by the headless drainer;
    # without these it read a generic `no-land` and the loop printed the misleading "inspect, then
    # re-run" instead of the `blocked` "GO DECIDE, then park"). "build option" (singular) matches
    # both "a build option" and "…build options". Only ever consulted on a NON-landed clean-tree
    # pass, so an incidental match in a LANDED pass's prose is harmless (landed is decided first).
    # (project_loop_next_todo_no_land_root_causes, 13th occurrence 2026-06-30.)
    "build option",
    "here are the options",
    "here are two options",
    # `/next-todo` Track-A SHAPE-FIRST escalation that REPORTS A RESEARCH CONCLUSION instead of
    # building: the pass investigates, surfaces a load-bearing ARCHITECTURE CONSTRAINT that means the
    # item needs a design decision, and stops — without any "build option" / "genuine fork" / "Track
    # A fork" wording. The real run-20260701T041052Z-76457 pass [5] (L2909 "Multi-factor discovery
    # sort" — a Track-A shape-first item the headless drainer picked) ended "I've confirmed the
    # architecture facts. Here's the key finding: yfinance's screen_equities() sort only accepts a
    # fixed scalar field name — it cannot sort …": none of the markers above match, terminal_signal
    # recorded null, so it read a generic `no-land` and the loop printed the misleading "inspect,
    # then re-run" (a re-run spin-stops the same way) instead of the `blocked` "GO DECIDE, then
    # park". "confirmed the architecture" catches the observed phrasing; "architectural constraint"
    # its common sibling. Only ever consulted on a NON-landed clean-tree pass, so a match in a LANDED
    # pass's prose is harmless (landed is decided first).
    # (project_loop_next_todo_no_land_root_causes, 14th occurrence 2026-07-01.)
    "confirmed the architecture",
    "architectural constraint",
)


# STRUCTURAL (vocabulary-independent) decision-fork signal. `_DECISION_FORK_MARKERS` above is a
# growing catalogue of EXACT phrases — a new one was bolted on for occurrences 8, 10, 13, 14, and 16,
# because each mis-escalated Track-A pass invents fresh wording. Occurrence 16 (run-20260702T205545Z
# pass [3]) ended: "Confirmed the design surface … The one genuine open decision — scope of this pass
# … (A) Foundation API only … (B) … (C) … I'll proceed with (A) unless you redirect. Reply 'go' (or
# A/B/C)" — and matched ZERO of the markers above, so it read a generic `no-land` and the loop printed
# the misleading "inspect, then re-run" (a re-run spin-stops the same way) instead of the `blocked`
# "GO DECIDE, then park". The durable fix is to key on the STRUCTURE these escalations SHARE, not
# their prose: they end by (a) laying out a lettered OPTION MENU for a human to pick from, and/or (b)
# soliciting a go/no-go the headless pass can't get. Both are phrasing-agnostic, so — unlike another
# marker — they don't age with the next escalation's vocabulary.
# (project_loop_next_todo_no_land_root_causes, 16th occurrence 2026-07-02.)
#
# Distinct lettered option labels — the canonical menu shape ("(A) …", "(B) …", "(C) …"). Plain
# substrings, not a regex, so no `re` import (the ruff-autofix import-strip trap:
# project_edit_hook_ruff_autofix_import_cascade).
_OPTION_MENU_LABELS: tuple[str, ...] = ("(a)", "(b)", "(c)", "(d)")
# Tail phrases that solicit the operator's direction. Consulted against the LAST slice of the text
# only, so a rhetorical "(shall we?)" mid-analysis doesn't trip it — the escalation's ASK is at the end.
_SOLICIT_DIRECTION_MARKERS: tuple[str, ...] = (
    "unless you redirect",
    "unless you say otherwise",
    "unless you tell me",
    'reply "go"',
    "reply 'go'",
    "reply go",
    "reply with a/b/c",
    "let me know which",
    "which would you",
    "which option",
    "shall i proceed",
    "should i proceed",
    "want me to proceed",
    "or redirect",
    "(a/b/c)",
    "(or a/b/c)",
)


def _looks_like_option_menu_escalation(text: str | None) -> bool:
    """Structural, vocabulary-independent decision-fork test: does the pass END by presenting a
    lettered option menu for a human to choose, or by soliciting a go/no-go it cannot get headless?

    The durable complement to `_DECISION_FORK_MARKERS` (which has needed a new EXACT phrase on five
    separate occurrences — 8/10/13/14/16). Only ever consulted on a NON-landed clean-tree pass, so a
    false match merely relabels one displayed word (no-land → blocked) — never control flow."""
    if not isinstance(text, str) or not text:
        return False
    s = text.lower()
    # A lettered option MENU the agent is asking a human to pick from, counted only in the LATTER
    # HALF of the turn (where a decision is presented) — an early "(a)…(b)…" enumeration inside a
    # mid-analysis aside is not an escalation. ≥2 DISTINCT labels = a menu, not an incidental "(a)".
    latter = s[len(s) // 2 :]
    if sum(1 for lbl in _OPTION_MENU_LABELS if lbl in latter) >= 2:
        return True
    # …or a tail that explicitly solicits the operator's go/no-go direction.
    return any(m in s[-400:] for m in _SOLICIT_DIRECTION_MARKERS)


def _looks_like_decision_fork(snippet: str | None) -> bool:
    """Heuristic: does a pass's final result text read as a DECISION-FORK escalation (the agent asking
    for a human call it can't make headless) rather than a genuine stop?

    Best-effort LABEL ENRICHMENT only — the no-work-to-recover sibling of `_looks_like_bg_yield`. It
    splits an already-"no progress" clean-tree, non-bg-yield no-land into `blocked` (the agent
    escalated a real fork — go decide, then park or build) vs a genuine `no-land`. A miss in either
    direction just changes one displayed word; never control flow.

    Two layers: the exact-phrase `_DECISION_FORK_MARKERS` fast-path, then the phrasing-agnostic
    `_looks_like_option_menu_escalation` structural fallback (the fix for the marker whack-a-mole —
    occurrence 16). An OR only ADDS matches, so every prior marker case still classifies `blocked`."""
    if not isinstance(snippet, str):
        return False
    s = snippet.lower()
    if any(m in s for m in _DECISION_FORK_MARKERS):
        return True
    return _looks_like_option_menu_escalation(snippet)


def _classify_terminal_signal(full_text: str | None) -> str | None:
    """Classify a pass's TERMINAL INTENT from the FULL (un-truncated) result text.

    Returns "bg_yield" (the agent ended its turn awaiting a background-job callback that
    headless `claude -p` never delivers), "decision_fork" (the agent escalated a human
    decision it can't make headless), or None (neither — a genuine stop / no signal).
    Checked bg_yield-FIRST to match `_pass_outcome`'s precedence (stalled before blocked).

    Run at RECORD time on the full result text so a marker that falls past the 157-char
    display-snippet truncation is still seen — the structural fix for the snippet-truncation
    blind spot that mislabelled the real L53 decision-fork as a generic `no-land`
    (project_loop_next_todo_no_land_root_causes, 10th occurrence). The single source of
    marker truth stays `_looks_like_bg_yield` / `_looks_like_decision_fork`; this only
    chooses WHICH text (full vs truncated) they run against and persists the verdict."""
    if not isinstance(full_text, str):
        return None
    if _looks_like_bg_yield(full_text):
        return "bg_yield"
    if _looks_like_decision_fork(full_text):
        return "decision_fork"
    return None


def _pass_outcome(rec: dict) -> str:
    """One-word outcome for a pass: landed | gate-fail | abandoned | stalled | blocked | no-land | ERROR."""
    claude = rec.get("claude") or {}
    if rec.get("rc", 0) != 0 or claude.get("is_error") is True:
        return "ERROR"
    if rec.get("landed"):
        # A landing whose persisted `Gate:` trailer command FAILED when re-run: origin/main
        # advanced (so it IS a landing — the commit is pushed) but the item's own deterministic
        # check says the goal did not actually land. gate_pass is tri-state: None = no trailer
        # on the landed range (recorded `gate:none`, NOT a failure — trailers are opt-in per
        # commit), True = green, False = red → gate-fail.
        if rec.get("gate_pass") is False:
            return "gate-fail"
        return "landed"
    # A clean exit that still left uncommitted work in the tree is an ABANDONED mid-land — the agent
    # yielded its turn before committing (classically a run_in_background job awaiting a callback that
    # headless `claude -p` never delivers). Distinct from a pass that genuinely did nothing; the loop
    # stops loudly on it, so the summary must not read "no-land". Absent on legacy records → no-land.
    if (rec.get("dirty_count") or 0) > 0:
        return "abandoned"
    # CLEAN tree, no land. The dirty-tree guard above only catches an abandonment that left EDITS;
    # an agent that yields during INVESTIGATION/validation — before any edit — leaves a clean tree
    # and slips past it. Two clean-tree no-progress sub-classes get their own label so the operator
    # gets the right next move: `stalled` (the agent abandoned mid-task awaiting a background-job
    # callback headless `claude -p` never delivers — re-run) and `blocked` (the agent ESCALATED a
    # genuine decision fork it can't resolve headless — it tried AskUserQuestion, found it
    # unavailable, surfaced the fork in prose — GO DECIDE, then park or rescope). Both are "no
    # progress"; only the displayed cause + next move differ.
    #
    # Prefer the persisted `terminal_signal`, classified at RECORD time from the FULL result text, so
    # a marker that fell past the 157-char display-snippet truncation is still seen (the truncation
    # blind spot that read the real L53 decision-fork as a generic `no-land` — 10th occurrence).
    # Legacy records that predate the field carry no `terminal_signal` (None) → fall back to scanning
    # the truncated snippet, exactly the prior behaviour. A new record with no signal also persists
    # None and falls back harmlessly (the snippet is a substring of the full text, so the fallback can
    # never surface a marker the full-text scan already missed).
    signal = claude.get("terminal_signal")
    if signal == "bg_yield":
        return "stalled"
    if signal == "decision_fork":
        return "blocked"
    if signal is None:
        if _looks_like_bg_yield(claude.get("result_snippet")):
            return "stalled"
        if _looks_like_decision_fork(claude.get("result_snippet")):
            return "blocked"
    # A genuine no-land — UNLESS the PR check failed AND git couldn't confirm either. origin/main's
    # SHA is the authoritative land anchor (git-only, immune to a gh blip), so when head_advanced is a
    # real bool the gh failure degrades only the PR LABEL — a merged PR moves origin/main, so
    # head_advanced=False definitively means nothing landed and "no-land" stays honest. Only when
    # head_advanced is None (legacy record, or git ALSO unresolvable) is the outcome truly
    # indeterminate: report "unknown" rather than asserting a no-land we can't prove.
    if rec.get("pr_check_failed") and rec.get("head_advanced") is None:
        return "unknown"
    return "no-land"


def _prs_label(rec: dict) -> str:
    prs = rec.get("new_prs") or []
    if prs:
        return " ".join(f"#{p.get('number')}·{(p.get('state') or '?').lower()}" for p in prs)
    # No PRs in the list: distinguish "gh genuinely found none" (—) from "gh couldn't be reached"
    # (unknown). The sentinel never folds into new_prs, so an empty list + pr_check_failed is the
    # could-not-check case.
    if rec.get("pr_check_failed"):
        return "unknown (gh unavailable)"
    return "—"


def _unmerged_work_note(rec: dict) -> str | None:
    """Surface in-flight work a non-landed pass left on a feature branch (the reused-PR blind spot).

    The land-signal only sees NEW PRs (number > pr_before) and the closed-count delta, so a pass that
    REUSES a pre-existing claude/* branch — whose PR predates the pass — can finish with real, unmerged
    work yet report as a pure no-land. When a non-landed pass ends on a claude/* branch that is ahead of
    origin/main OR carries an OPEN PR, name it explicitly so it is never silently filed as "nothing
    happened". Returns None when the pass landed, ended on main (any non-claude/* branch), or the branch
    is provably clean (0 commits ahead AND no open PR). An unknown ahead-count (None — an older record
    or a failed git probe) stays surfaced, conservatively.
    """
    if rec.get("landed"):
        return None
    branch = (rec.get("branch_end") or "").strip()
    if not branch.startswith("claude/"):
        return None
    ahead = rec.get("branch_commits_ahead")
    open_pr = rec.get("branch_open_pr")
    if not isinstance(open_pr, dict):
        open_pr = None
    # Suppress ONLY when we positively know nothing is unmerged: 0 commits ahead and no open PR.
    if isinstance(ahead, int) and ahead == 0 and not open_pr:
        return None
    parts: list[str] = []
    if open_pr:
        num = open_pr.get("number")
        state = (open_pr.get("state") or "OPEN").upper()
        parts.append(f"PR #{num} {state}" if num is not None else f"PR {state}")
    if isinstance(ahead, int) and ahead > 0:
        parts.append(f"{ahead} commit{'s' if ahead != 1 else ''} ahead of main")
    detail = ", ".join(parts) if parts else "unmerged"
    return f"⚠ unmerged work on {branch} ({detail})"


def _abandoned_work_note(rec: dict) -> str | None:
    """Surface UNcommitted work an abandoned pass left in the working tree — the sibling of
    _unmerged_work_note's committed-but-unmerged case. Returns a recover-or-discard prompt when a
    non-landed pass left a dirty tree, else None (landed, or a clean record / legacy record)."""
    if rec.get("landed"):
        return None
    n = rec.get("dirty_count") or 0
    if n <= 0:
        return None
    return f"⚠ {n} uncommitted file(s) left in the working tree — RECOVERABLE; land or discard before re-running"


# --- record ------------------------------------------------------------------


def _quoted_excerpt(rec: dict) -> str | None:
    """Fail-soft accessor for the persisted `terminal_excerpt`, quote-ready for a one-line note.

    None when absent (legacy record, or a landed/no-land pass that never had one persisted) or
    malformed (never raises — the loop's stats must render even on a hand-edited/corrupt JSONL
    record)."""
    claude = rec.get("claude")
    if not isinstance(claude, dict):
        return None
    excerpt = claude.get("terminal_excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return None
    # Already newline-flattened at persist time (_norm_claude); guard again in case a legacy or
    # hand-edited record carries raw newlines — a render must never crash the summary.
    return excerpt.strip().replace("\n", " ")


def _stalled_note(rec: dict) -> str | None:
    """Surface a CLEAN-tree mid-task background-job yield — the no-work-left sibling of
    _abandoned_work_note. Returns a re-run prompt naming the headless-callback cause when a
    non-landed, clean-tree pass classifies as `stalled`, else None (landed / dirty / genuine
    no-land / legacy). Quotes the persisted `terminal_excerpt` when present so the operator can see
    WHAT the agent was waiting on, not just that it stalled."""
    if rec.get("landed") or (rec.get("dirty_count") or 0) > 0:
        return None
    if _pass_outcome(rec) != "stalled":
        return None
    base = (
        "⚠ agent ended its turn awaiting a background-job callback `claude -p` never delivers "
        "(no work left — re-run; run probes/backfills/builds SYNCHRONOUSLY in-turn)"
    )
    excerpt = _quoted_excerpt(rec)
    return f'{base}: "{excerpt}"' if excerpt else base


def _blocked_note(rec: dict) -> str | None:
    """Surface a CLEAN-tree DECISION-FORK escalation — the agent hit a genuine fork it can't resolve
    headless and asked for a human call instead of guessing (the no-work-to-recover sibling of
    _stalled_note). Returns a go-decide prompt when a non-landed, clean-tree pass classifies as
    `blocked`, else None (landed / dirty / stalled / genuine no-land / legacy). Re-running won't help
    until the fork is resolved: PARK the item (trigger-gated) or rescope it so the loop can drain.
    Quotes the persisted `terminal_excerpt` when present so the operator can see the actual fork
    instead of only a generic go-decide prompt (project_loop_next_todo_no_land_root_causes, 10th
    occurrence — the L53 pass whose real fork was lost when the loop deleted its pass log)."""
    if rec.get("landed") or (rec.get("dirty_count") or 0) > 0:
        return None
    if _pass_outcome(rec) != "blocked":
        return None
    base = (
        "⚠ agent escalated a genuine decision fork it can't resolve headless (AskUserQuestion is "
        "unavailable under `claude -p`) — GO DECIDE, then PARK the item (trigger-gated) or rescope it "
        "so the loop drains past it; re-running unchanged will spin-stop here again"
    )
    excerpt = _quoted_excerpt(rec)
    return f'{base}: "{excerpt}"' if excerpt else base


def cmd_record(args: argparse.Namespace) -> int:
    agent = (getattr(args, "agent", None) or "claude").strip().lower() or "claude"
    # Historical JSONL key `claude` = normalized agent stats (claude OR grok). Keep the
    # key stable so summary/history/spin-guard keep reading older run files unchanged.
    claude = normalize_agent_result(args.result_file, agent=agent)

    new_prs = _try_json(args.new_prs_json) if args.new_prs_json else []
    # The shell `new_prs_json` emits the {"gh_error": true} sentinel when `gh pr list` could not be
    # reached (offline / auth blip / rate-limit) or its body was unparseable — distinct from "[]"
    # (gh genuinely found no new PRs). Record it as `pr_check_failed` so the PR column reads
    # "unknown (gh unavailable)" instead of "—" and the outcome reads "unknown" (not a definitive
    # "no-land") in the fully-degraded case. Any non-list (the sentinel, or malformed input) coerces
    # to an empty PR list so downstream readers stay simple.
    pr_check_failed = isinstance(new_prs, dict) and new_prs.get("gh_error") is True
    if not isinstance(new_prs, list):
        new_prs = []

    # The OPEN PR (if any) for branch_end, keyed on the branch — catches a REUSED PR whose number
    # predates the pass (so new_prs above, which filters number>pr_before, can't see it). None when
    # absent/unparsable/"null". Surfaced as in-flight unmerged work, never folded into new_prs.
    branch_open_pr = _try_json(args.branch_open_pr_json) if args.branch_open_pr_json else None
    if not isinstance(branch_open_pr, dict):
        branch_open_pr = None

    closed_before = args.closed_before
    closed_after = args.closed_after
    # AUTHORITATIVE land signal: origin/main advanced this pass (the loop pushed a commit). This is
    # immune to the `closed`-count proxy below, which only ticks when the agent strikes the item
    # IN PLACE *and keeps its `**Priority:**` line* — an agent that replaces that line with
    # `**Done:**` drops the item from BOTH the active and closed tallies, so a real, pushed landing
    # reads as no-land (the L178/L359 misfire that mislabelled two real commits + tripped the
    # spin-stop). The closed-delta + merged-PR checks stay as FALLBACKS for legacy records, where
    # the loop didn't pass head_advanced (None → falls through to them; behaviour unchanged).
    head_advanced = _bool_or_none(getattr(args, "head_advanced", None))
    landed = head_advanced is True
    if not landed:
        landed = (
            closed_after is not None and closed_before is not None and closed_after > closed_before
        )
    # A merged PR this pass also counts as landing even if the strike/sync
    # hasn't propagated into the local TODOS.md `closed` count yet.
    if not landed and any((p.get("state") or "").upper() == "MERGED" for p in new_prs):
        landed = True

    wall_s = None
    if args.started is not None and args.ended is not None:
        wall_s = max(0, args.ended - args.started)

    rec = {
        "iter": args.iter,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        # Which headless CLI produced this pass (`claude` | `grok`). Absent on legacy records.
        "agent": agent,
        "pick": {"priority": args.priority, "title": args.title, "line": args.line},
        "rc": args.rc,
        "started": args.started,
        "ended": args.ended,
        "wall_s": wall_s,
        "closed_before": closed_before,
        "closed_after": closed_after,
        "landed": landed,
        # Did origin/main advance this pass? The authoritative land anchor (None on legacy records).
        "head_advanced": head_advanced,
        # The landed range's `Gate:` commit-trailer verdict: None = no trailer (gate:none — not a
        # failure), True = the persisted check re-ran green, False = red (→ outcome `gate-fail`).
        # The trailer persists the item's /goalify check onto the landing commit so the drainer
        # verifies the GOAL landed, not just that a commit landed.
        "gate_pass": _bool_or_none(getattr(args, "gate_pass", None)),
        "gate_cmd": getattr(args, "gate_cmd", None) or None,
        "branch_end": args.branch_end,
        "branch_commits_ahead": args.branch_commits_ahead,
        "branch_open_pr": branch_open_pr,
        # PASS-ATTRIBUTABLE uncommitted file count at pass end — the abandoned-mid-land signal (the
        # loop stops loudly on >0). The driver set-diffs against a pre-pass porcelain snapshot, so
        # dirt that pre-dated the pass (another writer's) is EXCLUDED and never classifies the pass
        # `abandoned`. Absent on legacy records, which then read as no-land.
        "dirty_count": args.dirty_count,
        "new_prs": new_prs,
        # True when the PR check itself failed (gh unreachable) — distinct from "no new PRs". Absent
        # on legacy records → falsy, the prior behaviour. Drives the "unknown (gh unavailable)" label.
        "pr_check_failed": pr_check_failed,
        # Normalized agent stats (name kept for JSONL backcompat — not Claude-only).
        "claude": claude,
    }

    run_path = Path(args.run_file)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    with run_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")

    print(_render_pass_line(rec))
    return 0


def _render_pass_line(rec: dict) -> str:
    claude = rec.get("claude") or {}
    outcome = _pass_outcome(rec)
    mark = {
        "landed": "✓",
        "gate-fail": "✗",
        "abandoned": "⚠",
        "stalled": "⚠",
        "blocked": "⚠",
        "unknown": "?",
        "no-land": "•",
        "ERROR": "✗",
    }.get(outcome, "•")

    bits = [fmt_dur(rec.get("wall_s"))]
    if rec.get("rc", 0) != 0:
        bits.append(f"rc={rec['rc']}")
    if claude.get("parsed"):
        if claude.get("subtype") and claude.get("subtype") != "success":
            bits.append(str(claude["subtype"]))
        if claude.get("num_turns") is not None:
            bits.append(f"{claude['num_turns']} turns")
        bits.append(fmt_cost(claude.get("cost_usd")))
        tok = claude.get("tokens") or {}
        bits.append(f"{fmt_tokens(tok.get('input'))} in / {fmt_tokens(tok.get('output'))} out")
        cache = tok.get("cache_read") or 0
        if cache:
            bits.append(f"{fmt_tokens(cache)} cache")
        denials = claude.get("permission_denials") or 0
        if denials:
            bits.append(f"⚠ {denials} denials")
    else:
        bits.append("(no agent result parsed)")

    prs = _prs_label(rec)
    if prs != "—":
        bits.append(f"PR {prs}")

    line = f"  {mark} {outcome} · " + " · ".join(bits)

    if outcome == "gate-fail":
        cmd = rec.get("gate_cmd") or "<unknown gate>"
        line += (
            f"\n     ↳ ✗ gate red: `{cmd}` — the commit is pushed but the item's persisted "
            "check fails; inspect (or revert) the landed range"
        )

    note = _unmerged_work_note(rec)
    if note:
        line += f"\n     ↳ {note}"

    stalled = _stalled_note(rec)
    if stalled:
        line += f"\n     ↳ {stalled}"

    blocked = _blocked_note(rec)
    if blocked:
        line += f"\n     ↳ {blocked}"

    snippet = claude.get("result_snippet")
    if snippet:
        line += f"\n     ↳ {snippet}"
    return line


# --- summary -----------------------------------------------------------------


def _read_all_records(run_file: str) -> list[dict]:
    """Every dict record in the run JSONL — pass records AND `kind:autopark` events."""
    run_path = Path(run_file)
    if not run_path.exists():
        return []
    recs: list[dict] = []
    with run_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = _try_json(line)
            if isinstance(obj, dict):
                recs.append(obj)
    return recs


def _load_records(run_file: str) -> list[dict]:
    """PASS records only (the run's `claude -p` passes). Auto-park events (`kind:autopark`,
    written by the loop's decision-fork backstop) are EXCLUDED so every pass tally, the per-pass
    table, and `last-outcome` see only real passes — an auto-park is a loop action, not a pass."""
    return [r for r in _read_all_records(run_file) if r.get("kind") != "autopark"]


def _load_autoparks(run_file: str) -> list[dict]:
    """The `kind:autopark` events — the operator's decision queue (forks the loop parked to keep
    draining). Surfaced as its own end-of-run section, never mixed into the pass stats."""
    return [r for r in _read_all_records(run_file) if r.get("kind") == "autopark"]


def render_summary(
    recs: list[dict],
    *,
    reason: str | None = None,
    ready_remaining: str | None = None,
    run_file: str | None = None,
    autoparks: list[dict] | None = None,
) -> str:
    bar = "═" * 78
    out: list[str] = []
    out.append(bar)
    out.append(f"LOOP SUMMARY — {reason or 'finished'}")
    if run_file:
        out.append(f"  run file: {run_file}")

    if not recs:
        out.append("  no passes recorded.")
        out.append(bar)
        return "\n".join(out)

    landed = sum(1 for r in recs if _pass_outcome(r) == "landed")
    gate_fail = sum(1 for r in recs if _pass_outcome(r) == "gate-fail")
    abandoned = sum(1 for r in recs if _pass_outcome(r) == "abandoned")
    stalled = sum(1 for r in recs if _pass_outcome(r) == "stalled")
    blocked = sum(1 for r in recs if _pass_outcome(r) == "blocked")
    unknown = sum(1 for r in recs if _pass_outcome(r) == "unknown")
    no_land = sum(1 for r in recs if _pass_outcome(r) == "no-land")
    errored = sum(1 for r in recs if _pass_outcome(r) == "ERROR")

    total_wall = sum((r.get("wall_s") or 0) for r in recs)
    costs = [
        (r.get("claude") or {}).get("cost_usd")
        for r in recs
        if (r.get("claude") or {}).get("cost_usd") is not None
    ]
    total_cost = sum(costs) if costs else None
    total_turns = sum(((r.get("claude") or {}).get("num_turns") or 0) for r in recs)
    tok_in = sum(((r.get("claude") or {}).get("tokens") or {}).get("input", 0) for r in recs)
    tok_out = sum(((r.get("claude") or {}).get("tokens") or {}).get("output", 0) for r in recs)
    tok_cr = sum(((r.get("claude") or {}).get("tokens") or {}).get("cache_read", 0) for r in recs)
    tok_cc = sum(
        ((r.get("claude") or {}).get("tokens") or {}).get("cache_creation", 0) for r in recs
    )
    denials = sum(((r.get("claude") or {}).get("permission_denials") or 0) for r in recs)

    all_prs = [p for r in recs for p in (r.get("new_prs") or [])]
    prs_created = len({p.get("number") for p in all_prs if p.get("number") is not None})
    prs_merged = len(
        {
            p.get("number")
            for p in all_prs
            if (p.get("state") or "").upper() == "MERGED" and p.get("number") is not None
        }
    )

    closed_first = next(
        (r.get("closed_before") for r in recs if r.get("closed_before") is not None), None
    )
    closed_last = next(
        (r.get("closed_after") for r in reversed(recs) if r.get("closed_after") is not None),
        None,
    )

    n = len(recs)
    counts = f"  passes: {n}   landed: {landed}"
    if gate_fail:
        counts += f"   gate-fail: {gate_fail}"
    if abandoned:
        counts += f"   abandoned: {abandoned}"
    if stalled:
        counts += f"   stalled: {stalled}"
    if blocked:
        counts += f"   blocked: {blocked}"
    if unknown:
        counts += f"   unknown: {unknown}"
    counts += f"   no-landing: {no_land}   errored: {errored}"
    out.append(counts)
    if closed_first is not None and closed_last is not None:
        delta = closed_last - closed_first
        backlog = f"  backlog: closed {closed_first}→{closed_last} ({delta:+d})"
        if ready_remaining is not None:
            backlog += f"   ready remaining: {ready_remaining}"
        out.append(backlog)
    elif ready_remaining is not None:
        out.append(f"  ready remaining: {ready_remaining}")
    out.append(f"  wall-clock: {fmt_dur(total_wall)} total · {fmt_dur(total_wall / n)} avg/pass")
    if total_cost is not None:
        # Average over passes that actually reported a cost (len(costs) > 0 here), not all n —
        # an unparsed/crashed pass has no cost and would deflate a divide-by-n mean.
        out.append(
            f"  cost: {fmt_cost(total_cost)} total · {fmt_cost(total_cost / len(costs))} avg/reported-pass"
        )
    out.append(
        f"  tokens: {fmt_tokens(tok_in)} in · {fmt_tokens(tok_out)} out · "
        f"{fmt_tokens(tok_cr)} cache-read · {fmt_tokens(tok_cc)} cache-creation"
    )
    out.append(f"  turns: {total_turns} total · {total_turns / n:.1f} avg/pass")
    denial_note = "  permission denials: 0"
    if denials:
        denial_note = f"  permission denials: {denials}  ⚠ (agent hit the allowlist wall — inspect)"
    out.append(denial_note)
    out.append(f"  PRs: created {prs_created} · merged {prs_merged}")
    # Gate-trailer compliance across landings (landed + gate-fail): `none` = the landed range
    # carried no `Gate:` trailer — not a failure, but a drift signal if it dominates (the trailer
    # is how a landing proves the GOAL landed, not just a commit).
    landings = [r for r in recs if _pass_outcome(r) in ("landed", "gate-fail")]
    if landings:
        g_green = sum(1 for r in landings if r.get("gate_pass") is True)
        g_red = sum(1 for r in landings if r.get("gate_pass") is False)
        g_none = sum(1 for r in landings if r.get("gate_pass") is None)
        out.append(
            f"  gates: {g_green} green · {g_red} red · {g_none} none — "
            f"of {len(landings)} landing(s); none = no `Gate:` trailer on the landed range"
        )
    out.append("")

    # Per-pass table.
    header = f"  {'#':>2}  {'item':<34} {'wall':>9} {'turns':>5} {'cost':>8} {'den':>3}  {'outcome':<8} {'PRs'}"
    out.append(header)
    out.append("  " + "─" * (len(header) - 2))
    for r in recs:
        claude = r.get("claude") or {}
        title = r.get("pick", {}).get("title") or "?"
        out.append(
            f"  {r.get('iter', '?')!s:>2}  "
            f"{_truncate(title, 34):<34} "
            f"{fmt_dur(r.get('wall_s')):>9} "
            f"{claude.get('num_turns') if claude.get('num_turns') is not None else '—'!s:>5} "
            f"{fmt_cost(claude.get('cost_usd')):>8} "
            f"{claude.get('permission_denials') or 0!s:>3}  "
            f"{_pass_outcome(r):<8} "
            f"{_prs_label(r)}"
        )

    # In-flight / unmerged work — a non-landed pass that left commits or an OPEN PR on a claude/*
    # branch (typically a reused branch whose PR predates the pass, so it's invisible to the new-PR
    # land-signal). Surface it so a reused-PR pass isn't silently reported as a pure no-land.
    unmerged = [(r.get("iter", "?"), note) for r in recs if (note := _unmerged_work_note(r))]
    if unmerged:
        out.append("")
        out.append(
            "  ⚠ in-flight / unmerged work (NOT counted as landed — check for a reused branch/PR):"
        )
        for it, note in unmerged:
            out.append(f"    [{it}] {note}")

    # Abandoned mid-land — UNcommitted work an agent left in the working tree before ending its turn
    # (the loop stops loudly on this). The recover-or-discard sibling of the unmerged-work block above.
    abandoned_notes = [
        (r.get("iter", "?"), note) for r in recs if (note := _abandoned_work_note(r))
    ]
    if abandoned_notes:
        out.append("")
        out.append(
            "  ⚠ abandoned mid-land (uncommitted work the agent left before ending its turn):"
        )
        for it, note in abandoned_notes:
            out.append(f"    [{it}] {note}")

    # Stalled — a CLEAN-tree mid-task background-job yield (the agent ended its turn awaiting a
    # callback headless `claude -p` never delivers, before any edit). No work to recover, unlike the
    # abandoned block above; surfaced so it isn't filed identically to a genuine blocked no-land.
    stalled_notes = [(r.get("iter", "?"), note) for r in recs if (note := _stalled_note(r))]
    if stalled_notes:
        out.append("")
        out.append(
            "  ⚠ stalled (agent yielded mid-task for a background-job callback that never comes):"
        )
        for it, note in stalled_notes:
            out.append(f"    [{it}] {note}")

    # Blocked — a CLEAN-tree DECISION-FORK escalation: the agent hit a genuine fork it can't resolve
    # headless and asked for a human call instead of guessing. No work to recover (like stalled), but a
    # re-run won't help until the fork is resolved — so the next move is GO DECIDE, then park or rescope.
    blocked_notes = [(r.get("iter", "?"), note) for r in recs if (note := _blocked_note(r))]
    if blocked_notes:
        out.append("")
        out.append(
            "  ⚠ blocked (agent escalated a decision fork it can't resolve headless — go decide, then park/rescope):"
        )
        for it, note in blocked_notes:
            out.append(f"    [{it}] {note}")

    # Auto-parked — items the loop PARKED (trigger-gated) to keep draining past them: a `decision
    # fork` (go decide), a `bg-yield` stall (run the long op synchronously), or a generic `no-land`
    # (the pass ran the item and landed nothing with no stall/fork signal — inspect/rescope). This is
    # the operator's follow-up QUEUE: each was deferred, never resolved — clear it by hand. Loud +
    # itemized, and labeled by KIND so a stall or a no-land doesn't masquerade as a decision fork.
    if autoparks:
        # Map each event's raw park_kind to its display noun. Absent/unknown kind → "decision-fork"
        # (the legacy default, matching cmd_record_autopark). Counted per-kind so a single-kind run
        # keeps the terse "N <kind> item(s)" label and a mixed run enumerates every present kind.
        _KIND_LABEL = {"bg-yield": "stalled", "no-land": "no-land"}
        counts: dict[str, int] = {}
        for ap in autoparks:
            noun = _KIND_LABEL.get(ap.get("park_kind") or "decision fork", "decision-fork")
            counts[noun] = counts.get(noun, 0) + 1
        present = [(k, counts[k]) for k in ("decision-fork", "stalled", "no-land") if counts.get(k)]
        if len(present) == 1:
            label = f"{present[0][1]} {present[0][0]} item(s)"
        else:
            label = (
                f"{len(autoparks)} parked item(s) ("
                + ", ".join(f"{n} {k}" for k, n in present)
                + ")"
            )
        out.append("")
        out.append(
            f"  ⏸ auto-parked {label} — DECIDE + clear "
            "(the loop parked them to keep draining; the call is still yours):"
        )
        for ap in autoparks:
            pick = ap.get("pick") or {}
            title = pick.get("title") or "?"
            line = pick.get("line")
            loc = f"L{line} " if line else ""
            out.append(f"    • {loc}{title}")
            reason = ap.get("reason")
            if isinstance(reason, str) and reason.strip():
                out.append(f"        ↳ {_truncate(reason.strip(), 120)}")

    out.append(bar)
    return "\n".join(out)


def cmd_summary(args: argparse.Namespace) -> int:
    recs = _load_records(args.run_file)
    print(
        render_summary(
            recs,
            reason=args.reason,
            ready_remaining=args.ready_remaining,
            run_file=args.run_file,
            autoparks=_load_autoparks(args.run_file),
        )
    )
    return 0


def cmd_park_summary(args: argparse.Namespace) -> int:
    """Print a ONE-LINE reason for the auto-park marker, sourced from the LAST pass's persisted
    `terminal_excerpt` (the decision-fork text the blocked pass recorded). Empty when there is no
    excerpt (legacy/none) — the caller supplies a generic fallback. next_todo's `park` re-sanitizes
    and length-caps, so this only needs to be a single line."""
    recs = _load_records(args.run_file)
    reason = (_quoted_excerpt(recs[-1]) if recs else None) or ""
    reason = " ".join(reason.split())  # single line, collapsed whitespace
    if len(reason) > _PARK_SUMMARY_MAX:
        reason = reason[: _PARK_SUMMARY_MAX - 1].rstrip() + "…"
    print(reason)
    return 0


def cmd_record_autopark(args: argparse.Namespace) -> int:
    """Append a `kind:autopark` event to the run JSONL — the loud, durable record that the loop
    parked a decision-fork item to keep draining. Excluded from every pass tally (via
    `_load_records`) and surfaced as its own end-of-run decision queue (via `_load_autoparks`)."""
    rec = {
        "kind": "autopark",
        "iter": args.iter,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "pick": {"priority": args.priority, "title": args.title, "line": args.line},
        "reason": args.reason,
        # WHY the loop parked it, so the decision queue labels a stalled park honestly (not as a
        # "decision fork"). Absent on legacy records → treated as "decision fork" (the prior behaviour).
        "park_kind": getattr(args, "kind", None) or "decision fork",
    }
    run_path = Path(args.run_file)
    run_path.parent.mkdir(parents=True, exist_ok=True)
    with run_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Cross-RUN observability: aggregate every run-*.jsonl in the stats dir.

    The article-loop discipline is "run → observe where it stalls → iterate"; per-run summaries
    exist, but the recurring analysis ("which items keep not landing?") was manual JSONL
    archaeology across 20+ files. Prints (a) per-outcome totals across all runs, (b) total + avg
    cost per run, (c) repeat offenders — titles appearing in ≥2 STALLED/BLOCKED/NO-LAND/ABANDONED
    pass records or ≥2 autopark events. ERROR and unknown outcomes are EXCLUDED from offender
    counting: LOOP_ERROR_RETRIES deliberately re-records the same pick on transient failures, so
    one network outage would fabricate an offender. A title whose MOST RECENT pass record landed
    is kept but tagged "(resolved)". Fail-soft: an unreadable file is skipped, an empty dir prints
    a one-liner."""
    stats_dir = Path(args.stats_dir)
    run_files = sorted(stats_dir.glob("run-*.jsonl"))  # timestamped names → chronological
    if not run_files:
        print(f"no run-*.jsonl files under {stats_dir}")
        return 0

    outcome_totals: dict[str, int] = {}
    per_run: list[tuple[str, int, float | None]] = []  # (name, passes, cost)
    # title → [(ts_ordinal, outcome)] in chronological order (file order, then record order).
    title_hist: dict[str, list[str]] = {}
    autopark_counts: dict[str, int] = {}
    _OFFENDER_OUTCOMES = {"stalled", "blocked", "no-land", "abandoned"}

    for rf in run_files:
        recs = _load_records(str(rf))
        costs = [
            (r.get("claude") or {}).get("cost_usd")
            for r in recs
            if isinstance((r.get("claude") or {}).get("cost_usd"), (int, float))
        ]
        per_run.append((rf.name, len(recs), sum(costs) if costs else None))
        for r in recs:
            outcome = _pass_outcome(r)
            outcome_totals[outcome] = outcome_totals.get(outcome, 0) + 1
            title = (r.get("pick") or {}).get("title")
            if title:
                title_hist.setdefault(title, []).append(outcome)
        for ap in _load_autoparks(str(rf)):
            t = (ap.get("pick") or {}).get("title")
            if t:
                autopark_counts[t] = autopark_counts.get(t, 0) + 1

    bar = "═" * 78
    out: list[str] = [bar, f"LOOP HISTORY — {len(run_files)} run(s) under {stats_dir}"]
    out.append(
        "  outcomes: "
        + " · ".join(f"{k} {v}" for k, v in sorted(outcome_totals.items(), key=lambda kv: -kv[1]))
    )
    known_costs = [c for _, _, c in per_run if c is not None]
    if known_costs:
        out.append(
            f"  cost: {fmt_cost(sum(known_costs))} total · "
            f"{fmt_cost(sum(known_costs) / len(known_costs))} avg/run "
            f"({len(known_costs)}/{len(per_run)} runs reported)"
        )
    out.append("")
    out.append(f"  {'run':<38} {'passes':>6} {'cost':>9}")
    out.append("  " + "─" * 56)
    for name, n, cost in per_run:
        out.append(f"  {_truncate(name, 38):<38} {n:>6} {fmt_cost(cost):>9}")

    offenders: list[tuple[str, int, int, bool]] = []  # (title, bad_passes, parks, resolved)
    for title in set(title_hist) | set(autopark_counts):
        hist = title_hist.get(title, [])
        bad = sum(1 for o in hist if o in _OFFENDER_OUTCOMES)
        parks = autopark_counts.get(title, 0)
        if bad >= 2 or parks >= 2:
            resolved = bool(hist) and hist[-1] == "landed"
            offenders.append((title, bad, parks, resolved))
    if offenders:
        out.append("")
        out.append(
            "  repeat offenders (≥2 stalled/blocked/no-land/abandoned passes, or ≥2 auto-parks):"
        )
        for title, bad, parks, resolved in sorted(offenders, key=lambda t: -(t[1] + t[2])):
            tag = " (resolved)" if resolved else ""
            detail = f"{bad} non-landing pass(es)" + (f", {parks} auto-park(s)" if parks else "")
            out.append(f"    • {_truncate(title, 60)} — {detail}{tag}")
    out.append(bar)
    print("\n".join(out))
    return 0


def cmd_budget_check(args: argparse.Namespace) -> int:
    """Sum `cost_usd` over the run's pass records and compare against a run-level cap.

    Prints the total (a bare decimal) on stdout and exits 3 when total > max — the float
    comparison lives HERE because the caller is macOS bash 3.2, which cannot compare decimals.
    Fail-soft by design (exit 0, print 0.0000) on a missing/corrupt run-file or an unparseable
    cap: stats are never load-bearing — the per-pass `--max-budget-usd` remains the hard bound,
    this is the ADVISORY run-level aggregate on top. Crashed/unparsed passes report no cost_usd
    and are skipped, so the total can UNDER-count a run's true spend (an accounting aid, not a
    guarantee)."""
    recs = _load_records(args.run_file)
    total = 0.0
    for r in recs:
        cost = (r.get("claude") or {}).get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += cost
    print(f"{total:.4f}")
    try:
        max_usd = float(args.max_usd)
    except (TypeError, ValueError):
        return 0
    return 3 if total > max_usd else 0


def cmd_last_outcome(args: argparse.Namespace) -> int:
    """Print the one-word `_pass_outcome` of the LAST recorded pass (empty if none).

    The bash spin-guard reads this to tailor its STOP message: a re-pick after a `stalled`
    (background-job yield) or `blocked` (decision-fork) last pass must NOT advise a plain
    "re-run" — that reproduces the same stall — it must say "run the long op synchronously /
    park the item". Reuses the single-source `_pass_outcome` classifier so the stop line can
    never disagree with the per-pass line + summary table
    (project_loop_next_todo_no_land_root_causes, 11th occurrence — the clean-tree
    data-reextract bg-yield whose spin-stop still printed the generic "inspect, then re-run")."""
    recs = _load_records(args.run_file)
    print(_pass_outcome(recs[-1]) if recs else "")
    return 0


# --- cli ---------------------------------------------------------------------


def _int_or_none(value: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _bool_or_none(value: object) -> bool | None:
    """Tri-state flag: True/False when set, None when unset (legacy records → fall back to the
    closed-delta/PR signals). Idempotent — accepts an already-coerced bool (the test passes one
    directly) or the argparse string ("1"/"0")."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="append one pass record + print its console line")
    rec.add_argument("--run-file", required=True)
    rec.add_argument("--iter", type=int, required=True)
    rec.add_argument("--priority", default="")
    rec.add_argument("--title", default="")
    rec.add_argument("--line", type=_int_or_none, default=None)
    rec.add_argument("--rc", type=int, default=0)
    rec.add_argument("--started", type=_int_or_none, default=None)
    rec.add_argument("--ended", type=_int_or_none, default=None)
    rec.add_argument("--closed-before", type=_int_or_none, default=None)
    rec.add_argument("--closed-after", type=_int_or_none, default=None)
    rec.add_argument("--result-file", default=None)
    rec.add_argument(
        "--agent",
        default="claude",
        help="headless CLI that produced --result-file: claude (default) | grok",
    )
    rec.add_argument("--new-prs-json", default="")
    rec.add_argument("--branch-end", default=None)
    rec.add_argument("--branch-commits-ahead", type=_int_or_none, default=None)
    rec.add_argument("--branch-open-pr-json", default="")
    rec.add_argument("--dirty-count", type=_int_or_none, default=None)
    rec.add_argument("--head-advanced", default=None)
    rec.add_argument("--gate-pass", default=None, help="'' = no Gate: trailer; 1 = green; 0 = red")
    rec.add_argument("--gate-cmd", default=None)
    rec.set_defaults(func=cmd_record)

    summ = sub.add_parser("summary", help="render the end-of-run aggregate + table")
    summ.add_argument("--run-file", required=True)
    summ.add_argument("--reason", default=None)
    summ.add_argument("--ready-remaining", default=None)
    summ.set_defaults(func=cmd_summary)

    lo = sub.add_parser(
        "last-outcome", help="print the last pass's one-word outcome (for the spin-guard)"
    )
    lo.add_argument("--run-file", required=True)
    lo.set_defaults(func=cmd_last_outcome)

    bc = sub.add_parser(
        "budget-check",
        help="print the run's total cost and exit 3 when it exceeds --max-usd (advisory run cap)",
    )
    bc.add_argument("--run-file", required=True)
    bc.add_argument("--max-usd", required=True)
    bc.set_defaults(func=cmd_budget_check)

    hist = sub.add_parser(
        "history", help="aggregate all run-*.jsonl files: outcomes, cost/run, repeat offenders"
    )
    hist.add_argument("--stats-dir", default=".loop-runs")
    hist.set_defaults(func=cmd_history)

    ps = sub.add_parser(
        "park-summary", help="print a one-line auto-park reason from the last pass's excerpt"
    )
    ps.add_argument("--run-file", required=True)
    ps.set_defaults(func=cmd_park_summary)

    rap = sub.add_parser(
        "record-autopark", help="append a kind:autopark event to the run JSONL (the decision queue)"
    )
    rap.add_argument("--run-file", required=True)
    rap.add_argument("--iter", type=int, required=True)
    rap.add_argument("--priority", default="")
    rap.add_argument("--title", default="")
    rap.add_argument("--line", type=_int_or_none, default=None)
    rap.add_argument("--reason", default="")
    rap.add_argument(
        "--kind",
        default="decision fork",
        help="why it was parked: 'decision fork' (default) | 'bg-yield' (a stall)",
    )
    rap.set_defaults(func=cmd_record_autopark)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
