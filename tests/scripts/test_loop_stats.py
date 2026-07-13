"""Tests for scripts/loop_stats.py — the loop_next_todo.sh statistics helper.

The loop pushes all stats parsing/formatting here precisely so the brittle bits
are unit-tested instead of buried in bash: the `claude -p --output-format json`
result schema (which the loop can't validate), the landed?/PR detection, token
and cost aggregation, and the summary table. A regression here is a silent
mis-report — wrong cost, a landed pass shown as no-land, a crashed pass that
parses as success — so each surface gets a test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.loop_stats import (
    _TERMINAL_EXCERPT_MAX_CHARS,
    _blocked_note,
    _classify_terminal_signal,
    _looks_like_bg_yield,
    _looks_like_decision_fork,
    _looks_like_option_menu_escalation,
    _norm_claude,
    _norm_grok,
    _pass_outcome,
    _prs_label,
    _quoted_excerpt,
    _stalled_note,
    _unmerged_work_note,
    cmd_record,
    fmt_cost,
    fmt_dur,
    fmt_tokens,
    main,
    normalize_agent_result,
    parse_claude_result,
    parse_grok_result,
    render_summary,
)

# A realistic claude -p --output-format json result blob (keys mirror the live schema probed
# from `claude -p ... --output-format json`).
_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1694000,
    "duration_api_ms": 1500000,
    "num_turns": 11,
    "total_cost_usd": 1.8227,
    "session_id": "abc",
    "result": "Shipped PR #225.",
    "permission_denials": [],
    "usage": {
        "input_tokens": 142000,
        "output_tokens": 9000,
        "cache_read_input_tokens": 1_200_000,
        "cache_creation_input_tokens": 48000,
    },
}


# --- parse_claude_result -----------------------------------------------------


def test_parse_output_format_json(tmp_path: Path) -> None:
    f = tmp_path / "p.json"
    f.write_text(json.dumps(_RESULT), encoding="utf-8")
    got = parse_claude_result(str(f))
    assert got is not None
    assert got["total_cost_usd"] == 1.8227
    assert got["num_turns"] == 11


def test_parse_stream_json_takes_last_result(tmp_path: Path) -> None:
    """stream-json emits many events; the terminal type:result line is the summary."""
    f = tmp_path / "p.jsonl"
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": "thinking"}}),
        json.dumps({**_RESULT, "num_turns": 99}),  # the real final result
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = parse_claude_result(str(f))
    assert got is not None
    assert got["num_turns"] == 99


def test_parse_missing_file_is_none() -> None:
    assert parse_claude_result("/no/such/file.json") is None
    assert parse_claude_result(None) is None


def test_parse_empty_file_is_none(tmp_path: Path) -> None:
    f = tmp_path / "empty"
    f.write_text("", encoding="utf-8")
    assert parse_claude_result(str(f)) is None


def test_parse_garbage_is_none(tmp_path: Path) -> None:
    """A crashed pass with non-JSON stdout must not blow up — fail soft to None."""
    f = tmp_path / "garbage"
    f.write_text("Traceback (most recent call last):\n  boom\n", encoding="utf-8")
    assert parse_claude_result(str(f)) is None


# --- parse_grok_result + _norm_grok ------------------------------------------

_GROK_RESULT = {
    "text": "Shipped the fix and pushed to main.",
    "stopReason": "EndTurn",
    "sessionId": "g-abc",
    "requestId": "req-1",
    "thought": "done",
}


def test_parse_grok_output_format_json(tmp_path: Path) -> None:
    f = tmp_path / "g.json"
    f.write_text(json.dumps(_GROK_RESULT), encoding="utf-8")
    got = parse_grok_result(str(f))
    assert got is not None
    assert got["text"] == _GROK_RESULT["text"]
    assert got["stopReason"] == "EndTurn"


def test_parse_grok_error_object(tmp_path: Path) -> None:
    f = tmp_path / "g-err.json"
    f.write_text(json.dumps({"type": "error", "message": "API Error: Connection closed"}), encoding="utf-8")
    got = parse_grok_result(str(f))
    assert got is not None
    assert got["type"] == "error"
    n = _norm_grok(got)
    assert n["parsed"] is True
    assert n["is_error"] is True
    assert n["subtype"] == "error"
    assert "Connection closed" in (n["result_snippet"] or "")


def test_parse_grok_streaming_json_assembles_text(tmp_path: Path) -> None:
    f = tmp_path / "g.jsonl"
    lines = [
        json.dumps({"type": "text", "data": "Hello "}),
        json.dumps({"type": "thought", "data": "thinking"}),
        json.dumps({"type": "text", "data": "world."}),
        json.dumps({"type": "end", "stopReason": "EndTurn", "sessionId": "s1"}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    got = parse_grok_result(str(f))
    assert got is not None
    assert got["text"] == "Hello world."
    assert got["stopReason"] == "EndTurn"
    n = _norm_grok(got)
    assert n["parsed"] is True
    assert n["is_error"] is False
    assert n["result_snippet"] == "Hello world."


def test_norm_grok_classifies_decision_fork() -> None:
    text = (
        "Investigated the item. AskUserQuestion isn't available here, so I'll surface this in "
        "prose — it's a genuine fork worth your call before I build: option A vs option B."
    )
    n = _norm_grok({**_GROK_RESULT, "text": text})
    assert n["terminal_signal"] == "decision_fork"
    assert n["terminal_excerpt"] == text


def test_normalize_agent_result_dispatches(tmp_path: Path) -> None:
    cf = tmp_path / "c.json"
    cf.write_text(json.dumps(_RESULT), encoding="utf-8")
    gf = tmp_path / "g.json"
    gf.write_text(json.dumps(_GROK_RESULT), encoding="utf-8")
    cn = normalize_agent_result(str(cf), agent="claude")
    gn = normalize_agent_result(str(gf), agent="grok")
    assert cn["parsed"] is True and cn["num_turns"] == 11
    assert gn["parsed"] is True and "Shipped" in (gn["result_snippet"] or "")
    # Unknown agent falls through to claude parser (fail-soft).
    assert normalize_agent_result(str(cf), agent="unknown")["parsed"] is True


def test_cmd_record_stores_agent_field(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    resf = tmp_path / "pass.json"
    resf.write_text(json.dumps(_GROK_RESULT), encoding="utf-8")
    cmd_record(
        _Args(
            run_file=str(rf),
            result_file=str(resf),
            agent="grok",
            head_advanced="1",
            closed_before=1,
            closed_after=2,
        )
    )
    rec = json.loads(rf.read_text(encoding="utf-8").strip())
    assert rec["agent"] == "grok"
    assert rec["claude"]["parsed"] is True
    assert "Shipped" in (rec["claude"]["result_snippet"] or "")
    assert rec["landed"] is True


# --- _norm_claude ------------------------------------------------------------


def test_norm_claude_extracts_fields() -> None:
    n = _norm_claude(_RESULT)
    assert n["parsed"] is True
    assert n["num_turns"] == 11
    assert n["cost_usd"] == 1.8227
    assert n["tokens"] == {
        "input": 142000,
        "output": 9000,
        "cache_read": 1_200_000,
        "cache_creation": 48000,
    }
    assert n["permission_denials"] == 0


def test_norm_claude_counts_permission_denials() -> None:
    n = _norm_claude({**_RESULT, "permission_denials": [{"tool": "Bash"}, {"tool": "Bash"}]})
    assert n["permission_denials"] == 2


def test_norm_claude_handles_missing_usage() -> None:
    n = _norm_claude({"type": "result", "subtype": "success"})
    assert n["parsed"] is True
    assert n["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    assert n["cost_usd"] is None


@pytest.mark.parametrize("bad_usage", [[{"input_tokens": 1}], "corrupt", 42, True])
def test_norm_claude_tolerates_non_dict_usage(bad_usage: object) -> None:
    """A truthy-but-malformed `usage` (list/str/number from a partial result) must NOT raise —
    an `or {}` guard would let it through and AttributeError, silently dropping the whole pass."""
    n = _norm_claude({**_RESULT, "usage": bad_usage})
    assert n["parsed"] is True
    assert n["tokens"] == {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}


def test_norm_claude_none_is_unparsed() -> None:
    assert _norm_claude(None) == {"parsed": False}


def test_norm_claude_truncates_snippet() -> None:
    long = "x" * 500
    n = _norm_claude({**_RESULT, "result": long})
    assert n["result_snippet"].endswith("…")
    assert len(n["result_snippet"]) <= 160


def test_norm_claude_flattens_newlines_in_snippet() -> None:
    n = _norm_claude({**_RESULT, "result": "line one\nline two"})
    assert "\n" not in n["result_snippet"]


# --- terminal_excerpt: quotable full text for a blocked/stalled pass ---------


def test_norm_claude_persists_terminal_excerpt_on_decision_fork() -> None:
    """A decision-fork classification persists the FULL result text as `terminal_excerpt`
    (flattened) — not just the 157-char display snippet. This is the whole point of the field: a
    blocked pass's note can now quote the actual fork instead of only a generic go-decide prompt."""
    text = (
        "Investigated the item. AskUserQuestion isn't available here, so I'll surface this in "
        "prose — it's a genuine fork worth your call before I build: option A vs option B."
    )
    n = _norm_claude({**_RESULT, "result": text})
    assert n["terminal_signal"] == "decision_fork"
    assert n["terminal_excerpt"] == text


def test_norm_claude_persists_terminal_excerpt_on_bg_yield() -> None:
    text = (
        "Kicked off the backfill running in the background; I'll await its completion "
        "notification rather than poll."
    )
    n = _norm_claude({**_RESULT, "result": text})
    assert n["terminal_signal"] == "bg_yield"
    assert n["terminal_excerpt"] == text


def test_norm_claude_terminal_excerpt_none_without_signal() -> None:
    """A benign landed/no-land result has no fork to quote — the field stays None so a multi-pass
    run's JSONL doesn't bloat with excerpts nobody will read (per the ticket's don't-bloat-every-
    record constraint)."""
    n = _norm_claude({**_RESULT, "result": "Shipped the fix and pushed to main."})
    assert n["terminal_signal"] is None
    assert n["terminal_excerpt"] is None


def test_norm_claude_terminal_excerpt_caps_length() -> None:
    long_fork = (
        "AskUserQuestion isn't available, so surfacing in prose — genuine fork worth your call. "
        + "x" * 3000
    )
    n = _norm_claude({**_RESULT, "result": long_fork})
    assert n["terminal_signal"] == "decision_fork"
    assert n["terminal_excerpt"].endswith("…")
    assert len(n["terminal_excerpt"]) <= _TERMINAL_EXCERPT_MAX_CHARS


def test_norm_claude_flattens_newlines_in_terminal_excerpt() -> None:
    text = (
        "AskUserQuestion isn't available, so surfacing this in prose.\nThis is a genuine fork "
        "worth your call."
    )
    n = _norm_claude({**_RESULT, "result": text})
    assert n["terminal_signal"] == "decision_fork"
    assert "\n" not in n["terminal_excerpt"]


# --- _quoted_excerpt: fail-soft accessor -------------------------------------


def test_quoted_excerpt_returns_none_when_absent() -> None:
    assert _quoted_excerpt({"claude": {}}) is None
    assert _quoted_excerpt({}) is None


@pytest.mark.parametrize("bad", [None, 42, [], {}, "   "])
def test_quoted_excerpt_fails_soft_on_malformed_value(bad: object) -> None:
    """A hand-edited or corrupt JSONL record must not crash the summary render."""
    assert _quoted_excerpt({"claude": {"terminal_excerpt": bad}}) is None


@pytest.mark.parametrize("bad_claude", [None, "oops", 42, []])
def test_quoted_excerpt_fails_soft_on_non_dict_claude(bad_claude: object) -> None:
    """A corrupt record whose `claude` value itself isn't a dict must not raise — a plain
    `.get("claude") or {}` guard would still call `.get()` on a truthy non-dict and crash."""
    assert _quoted_excerpt({"claude": bad_claude}) is None


def test_quoted_excerpt_strips_and_reflattens_legacy_newlines() -> None:
    """Belt-and-suspenders: even if a legacy/hand-edited record carries raw newlines, the
    accessor still returns a one-line string (persist-time flattening is the primary guard)."""
    got = _quoted_excerpt({"claude": {"terminal_excerpt": "  line one\nline two  "}})
    assert got == "line one line two"


# --- formatting --------------------------------------------------------------


def test_fmt_dur() -> None:
    assert fmt_dur(0) == "0m00s"
    assert fmt_dur(65) == "1m05s"
    assert fmt_dur(3661) == "1h01m01s"
    assert fmt_dur(None) == "—"


def test_fmt_tokens() -> None:
    assert fmt_tokens(0) == "0"
    assert fmt_tokens(950) == "950"
    assert fmt_tokens(12_000) == "12k"
    assert fmt_tokens(1_200_000) == "1.2M"


def test_fmt_cost() -> None:
    assert fmt_cost(None) == "$—"
    assert fmt_cost(1.827) == "$1.83"
    assert fmt_cost(0) == "$0.00"


# --- landed detection (via cmd_record) ---------------------------------------


class _Args:
    """Minimal stand-in for the argparse namespace cmd_record consumes."""

    def __init__(self, **kw: object) -> None:
        defaults = {
            "run_file": None,
            "iter": 1,
            "priority": "P1",
            "title": "Some item",
            "line": 100,
            "rc": 0,
            "started": 1000,
            "ended": 1100,
            "closed_before": None,
            "closed_after": None,
            "result_file": None,
            "new_prs_json": "",
            "branch_end": "main",
            "branch_commits_ahead": None,
            "branch_open_pr_json": "",
            "dirty_count": None,
            "head_advanced": None,
        }
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


def _read_one(run_file: Path) -> dict:
    lines = [ln for ln in run_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def test_record_landed_on_closed_delta(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), closed_before=39, closed_after=40))
    rec = _read_one(rf)
    assert rec["landed"] is True
    assert _pass_outcome(rec) == "landed"


def test_record_not_landed_when_closed_flat(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), closed_before=40, closed_after=40))
    assert _read_one(rf)["landed"] is False


def test_record_landed_when_pr_merged_even_if_closed_flat(tmp_path: Path) -> None:
    """A merged PR this pass counts as landing even before the strike syncs into local TODOS."""
    rf = tmp_path / "run.jsonl"
    prs = json.dumps([{"number": 225, "title": "x", "state": "MERGED", "url": "u"}])
    cmd_record(_Args(run_file=str(rf), closed_before=40, closed_after=40, new_prs_json=prs))
    assert _read_one(rf)["landed"] is True


def test_record_open_pr_does_not_count_as_landed(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    prs = json.dumps([{"number": 226, "title": "x", "state": "OPEN", "url": "u"}])
    cmd_record(_Args(run_file=str(rf), closed_before=40, closed_after=40, new_prs_json=prs))
    assert _read_one(rf)["landed"] is False


def test_record_landed_on_head_advanced_even_when_closed_flat(tmp_path: Path) -> None:
    """The L178/L359 misfire: a real commit landed on origin/main, but the agent struck the item
    DONE in a way that dropped its `**Priority:**` line, so the `closed`-count proxy stayed flat.
    head_advanced is authoritative — the pass MUST read as landed, not no-land."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), closed_before=40, closed_after=40, head_advanced="1"))
    rec = _read_one(rf)
    assert rec["landed"] is True
    assert rec["head_advanced"] is True
    assert _pass_outcome(rec) == "landed"


def test_record_head_not_advanced_is_no_land_when_nothing_else_signals(tmp_path: Path) -> None:
    """origin/main did NOT move and nothing else landed → a genuine no-land (the real spin case)."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), closed_before=40, closed_after=40, head_advanced="0"))
    rec = _read_one(rf)
    assert rec["landed"] is False
    assert rec["head_advanced"] is False
    assert _pass_outcome(rec) == "no-land"


def test_record_legacy_none_head_advanced_falls_back_to_closed_delta(tmp_path: Path) -> None:
    """Legacy records (loop didn't pass head_advanced) keep the old closed-delta behaviour."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), closed_before=39, closed_after=40, head_advanced=None))
    rec = _read_one(rf)
    assert rec["landed"] is True
    assert rec["head_advanced"] is None


def test_record_nonzero_rc_is_error_outcome(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), rc=124, closed_before=1, closed_after=2))
    rec = _read_one(rf)
    # Even with a closed-count bump, a non-zero exit is an ERROR pass, not a clean landing.
    assert _pass_outcome(rec) == "ERROR"


def test_record_appends_not_overwrites(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), iter=1))
    cmd_record(_Args(run_file=str(rf), iter=2))
    lines = [ln for ln in rf.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [json.loads(ln)["iter"] for ln in lines] == [1, 2]


def test_record_bad_new_prs_json_is_tolerated(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), new_prs_json="not json", closed_before=1, closed_after=1))
    rec = _read_one(rf)
    assert rec["new_prs"] == []
    # Malformed-but-not-the-sentinel input is NOT a gh-check failure — it stays a plain empty list.
    assert rec["pr_check_failed"] is False


# --- gh-check-failed sentinel ({"gh_error": true}) ---------------------------


def test_record_gh_error_sentinel_sets_pr_check_failed(tmp_path: Path) -> None:
    """The shell new_prs_json emits {"gh_error": true} when `gh pr list` is unreachable —
    distinct from "[]" (no new PRs). cmd_record records pr_check_failed and keeps new_prs empty."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), new_prs_json='{"gh_error": true}', head_advanced="0"))
    rec = _read_one(rf)
    assert rec["pr_check_failed"] is True
    assert rec["new_prs"] == []


def test_record_plain_empty_prs_is_not_a_check_failure(tmp_path: Path) -> None:
    """An empty array (gh genuinely found no new PRs) must NOT read as a check failure."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), new_prs_json="[]", head_advanced="0"))
    rec = _read_one(rf)
    assert rec["pr_check_failed"] is False
    assert rec["new_prs"] == []


def test_prs_label_distinguishes_unavailable_from_none() -> None:
    # gh found nothing → em-dash; gh unreachable → explicit "unknown"; PRs present → the labels.
    assert _prs_label({"new_prs": [], "pr_check_failed": False}) == "—"
    assert _prs_label({"new_prs": [], "pr_check_failed": True}) == "unknown (gh unavailable)"
    assert _prs_label({}) == "—"  # legacy record (no field) → prior behaviour
    assert (
        _prs_label({"new_prs": [{"number": 225, "state": "MERGED"}], "pr_check_failed": True})
        == "#225·merged"
    )


def test_outcome_unknown_only_when_gh_failed_and_git_cannot_confirm(tmp_path: Path) -> None:
    """gh check failed AND origin/main was unresolvable (head_advanced None) → the land is truly
    indeterminate: report "unknown", not a false "no-land"."""
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(
            run_file=str(rf), new_prs_json='{"gh_error": true}', head_advanced=None, dirty_count=0
        )
    )
    rec = _read_one(rf)
    assert rec["landed"] is False
    assert rec["head_advanced"] is None
    assert _pass_outcome(rec) == "unknown"


def test_outcome_stays_no_land_when_git_authoritatively_says_no_move(tmp_path: Path) -> None:
    """Even with the gh check failed, head_advanced=False means origin/main did NOT move — a merge
    would have advanced it — so nothing landed. git is authoritative; keep the definite "no-land"."""
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(run_file=str(rf), new_prs_json='{"gh_error": true}', head_advanced="0", dirty_count=0)
    )
    rec = _read_one(rf)
    assert rec["head_advanced"] is False
    assert _pass_outcome(rec) == "no-land"


def test_outcome_landed_wins_over_gh_error(tmp_path: Path) -> None:
    """A confirmed landing (origin/main advanced) is "landed" regardless of a gh-check blip."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), new_prs_json='{"gh_error": true}', head_advanced="1"))
    rec = _read_one(rf)
    assert _pass_outcome(rec) == "landed"


def test_record_parses_claude_result_file(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    resf = tmp_path / "res.json"
    resf.write_text(json.dumps(_RESULT), encoding="utf-8")
    cmd_record(_Args(run_file=str(rf), result_file=str(resf)))
    rec = _read_one(rf)
    assert rec["claude"]["cost_usd"] == 1.8227
    assert rec["claude"]["tokens"]["cache_read"] == 1_200_000


def test_record_persists_branch_unmerged_fields(tmp_path: Path) -> None:
    """cmd_record stores branch_commits_ahead + the branch-keyed OPEN PR (the reused-branch signal)."""
    rf = tmp_path / "run.jsonl"
    open_pr = json.dumps({"number": 226, "title": "x", "state": "OPEN", "url": "u"})
    cmd_record(
        _Args(
            run_file=str(rf),
            branch_end="claude/foo-123",
            branch_commits_ahead=4,
            branch_open_pr_json=open_pr,
            closed_before=40,
            closed_after=40,
        )
    )
    rec = _read_one(rf)
    assert rec["branch_commits_ahead"] == 4
    assert rec["branch_open_pr"]["number"] == 226
    assert rec["branch_open_pr"]["state"] == "OPEN"


def test_record_branch_open_pr_null_or_garbage_is_none(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), branch_end="main", branch_open_pr_json="null"))
    assert _read_one(rf)["branch_open_pr"] is None
    rf2 = tmp_path / "run2.jsonl"
    cmd_record(_Args(run_file=str(rf2), branch_end="main", branch_open_pr_json="not json"))
    assert _read_one(rf2)["branch_open_pr"] is None


# --- unmerged-work detection (reused-branch / reused-PR blind spot) -----------


def test_unmerged_note_open_pr_on_reused_branch() -> None:
    """A non-landed pass left on a claude/* branch with an OPEN PR is surfaced (the reused-PR case)."""
    rec = {
        "landed": False,
        "branch_end": "claude/foo-123",
        "branch_commits_ahead": 3,
        "branch_open_pr": {"number": 226, "state": "OPEN"},
    }
    note = _unmerged_work_note(rec)
    assert note is not None
    assert "claude/foo-123" in note
    assert "PR #226 OPEN" in note
    assert "3 commits ahead" in note


def test_unmerged_note_commits_ahead_no_pr() -> None:
    rec = {
        "landed": False,
        "branch_end": "claude/bar-9",
        "branch_commits_ahead": 1,
        "branch_open_pr": None,
    }
    note = _unmerged_work_note(rec)
    assert note is not None
    assert "1 commit ahead" in note  # singular
    assert "PR" not in note


def test_unmerged_note_none_when_landed() -> None:
    rec = {
        "landed": True,
        "branch_end": "claude/foo-123",
        "branch_commits_ahead": 5,
        "branch_open_pr": {"number": 226, "state": "OPEN"},
    }
    assert _unmerged_work_note(rec) is None


def test_unmerged_note_none_on_main() -> None:
    rec = {
        "landed": False,
        "branch_end": "main",
        "branch_commits_ahead": None,
        "branch_open_pr": None,
    }
    assert _unmerged_work_note(rec) is None


def test_unmerged_note_none_when_provably_clean() -> None:
    """0 commits ahead AND no open PR on the claude/* branch means nothing unmerged — stay silent."""
    rec = {
        "landed": False,
        "branch_end": "claude/foo-123",
        "branch_commits_ahead": 0,
        "branch_open_pr": None,
    }
    assert _unmerged_work_note(rec) is None


def test_unmerged_note_unknown_ahead_stays_surfaced() -> None:
    """An unknown ahead-count (None — older record or failed git probe) is surfaced conservatively."""
    rec = {
        "landed": False,
        "branch_end": "claude/foo-123",
        "branch_commits_ahead": None,
        "branch_open_pr": None,
    }
    note = _unmerged_work_note(rec)
    assert note is not None
    assert "claude/foo-123" in note


def test_unmerged_note_errored_pass_on_branch_surfaced() -> None:
    """A crashed pass (rc!=0, not landed) that stranded work on a claude/* branch is still surfaced."""
    rec = {
        "landed": False,
        "rc": 124,
        "branch_end": "claude/foo-123",
        "branch_commits_ahead": 2,
        "branch_open_pr": None,
    }
    assert _unmerged_work_note(rec) is not None


# --- abandoned mid-land detection (uncommitted blind spot) -------------------


def test_record_persists_dirty_count(tmp_path: Path) -> None:
    """cmd_record stores the post-pass uncommitted-file count (the abandoned-mid-land signal)."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), dirty_count=7, closed_before=40, closed_after=40))
    assert _read_one(rf)["dirty_count"] == 7


def test_outcome_dirty_tree_non_landed_is_abandoned() -> None:
    """A clean exit that still left uncommitted work is 'abandoned', not 'no-land'."""
    rec = {"rc": 0, "landed": False, "dirty_count": 3, "claude": {"is_error": False}}
    assert _pass_outcome(rec) == "abandoned"


def test_outcome_landed_beats_dirty() -> None:
    """A landed pass is 'landed' even if it left stray uncommitted files."""
    rec = {"rc": 0, "landed": True, "dirty_count": 3, "claude": {"is_error": False}}
    assert _pass_outcome(rec) == "landed"


def test_outcome_errored_beats_dirty() -> None:
    """A crashed pass (rc!=0) reads 'ERROR' regardless of a dirty tree."""
    rec = {"rc": 124, "landed": False, "dirty_count": 3, "claude": {"is_error": False}}
    assert _pass_outcome(rec) == "ERROR"


def test_outcome_clean_non_landed_is_no_land() -> None:
    rec = {"rc": 0, "landed": False, "dirty_count": 0, "claude": {"is_error": False}}
    assert _pass_outcome(rec) == "no-land"


def test_outcome_legacy_record_without_dirty_count_is_no_land() -> None:
    """A pre-feature record (no dirty_count key), non-landed, stays 'no-land' — no false abandon."""
    rec = {"rc": 0, "landed": False, "claude": {"is_error": False}}
    assert _pass_outcome(rec) == "no-land"


# --- stalled detection (clean-tree background-job yield) ----------------------

# The EXACT final-result snippet from the real no-land that prompted this branch:
# .loop-runs/run-20260624T195819Z-71130.jsonl pass [2] (the IFRS value-fidelity probe). The agent
# yielded during INVESTIGATION awaiting a background-job callback headless `claude -p` never
# delivers, leaving a CLEAN tree — so the dirty-tree `abandoned` guard couldn't see it. The oracle
# is this externally-observed failure, not anything the classifier itself produces.
_PASS2_STALL_SNIPPET = (
    "Probe is working through the filings (printing amended-filing warnings as it goes). "
    "I'll await its completion notification rather than poll."
)


def test_real_pass2_clean_tree_yield_is_stalled_not_no_land() -> None:
    """REGRESSION: the real 6th-occurrence no-land (clean tree, agent yielded mid-investigation)
    must classify as 'stalled', not collapse into a generic 'no-land' that blames the item."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS2_STALL_SNIPPET},
    }
    assert _pass_outcome(rec) == "stalled"


def test_genuine_blocked_no_land_stays_no_land() -> None:
    """A clean-tree no-land whose result reads as a genuine stop (blocked / deferred / no-op) stays
    'no-land' — the heuristic must not flag every no-land as a stall."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": "The top item is BLOCKED on a labeled calibration set that does "
            "not exist yet; deferring and parking it with a Depends-on line.",
        },
    }
    assert _pass_outcome(rec) == "no-land"


def test_clean_tree_no_snippet_is_no_land() -> None:
    """No result snippet (unparsed result) → fail soft to no-land, never a false stall."""
    rec = {"rc": 0, "landed": False, "dirty_count": 0, "claude": {"is_error": False}}
    assert _pass_outcome(rec) == "no-land"


def test_dirty_tree_beats_stall_snippet() -> None:
    """A bg-yield snippet WITH a dirty tree is 'abandoned' (recoverable work) — the dirty-tree
    branch takes precedence so the work-recovery prompt always fires."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 4,
        "claude": {"is_error": False, "result_snippet": _PASS2_STALL_SNIPPET},
    }
    assert _pass_outcome(rec) == "abandoned"


def test_landed_beats_stall_snippet() -> None:
    """A landed pass is 'landed' even if its result text happens to mention a background job."""
    rec = {
        "rc": 0,
        "landed": True,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS2_STALL_SNIPPET},
    }
    assert _pass_outcome(rec) == "landed"


@pytest.mark.parametrize(
    "snippet",
    [
        _PASS2_STALL_SNIPPET,
        "I'll await the completion notification.",
        "Kicked off the backfill running in the background; will resume once it finishes.",
        "Leaving it to run in the background.",
        "I will wait for it to complete and then commit.",
    ],
)
def test_looks_like_bg_yield_matches_yield_phrasing(snippet: str) -> None:
    assert _looks_like_bg_yield(snippet) is True


@pytest.mark.parametrize(
    "snippet",
    [
        None,
        "",
        "Shipped the fix and pushed to main.",
        "Item is blocked; deferred with a Depends-on line.",
        "No READY item left in the backlog.",
    ],
)
def test_looks_like_bg_yield_ignores_non_yield_text(snippet: str | None) -> None:
    assert _looks_like_bg_yield(snippet) is False


def test_stalled_note_only_for_stalled_clean_pass() -> None:
    stalled = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS2_STALL_SNIPPET},
    }
    note = _stalled_note(stalled)
    assert note is not None
    assert "background-job callback" in note
    # Not for landed, dirty (abandoned), or genuine no-land passes.
    assert _stalled_note({**stalled, "landed": True}) is None
    assert _stalled_note({**stalled, "dirty_count": 5}) is None
    assert (
        _stalled_note(
            {"rc": 0, "landed": False, "dirty_count": 0, "claude": {"result_snippet": "shipped"}}
        )
        is None
    )


def test_stalled_note_quotes_terminal_excerpt_when_present() -> None:
    """THE FIX: a new record (with `terminal_excerpt` persisted) gets the actual awaited-callback
    text quoted in the note, not just the generic re-run prompt."""
    excerpt = "Kicked off the reextract backfill; awaiting its completion notification."
    stalled = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": _PASS2_STALL_SNIPPET,
            "terminal_excerpt": excerpt,
        },
    }
    note = _stalled_note(stalled)
    assert note is not None
    assert excerpt in note


def test_stalled_note_falls_back_to_generic_when_excerpt_absent() -> None:
    """A legacy record (predates `terminal_excerpt`) still gets the generic prompt — no crash, no
    dangling ': "None"' in the note."""
    legacy = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS2_STALL_SNIPPET},
    }
    note = _stalled_note(legacy)
    assert note is not None
    assert "None" not in note
    assert note.endswith("in-turn)")


# --- blocked detection (clean-tree decision-fork escalation) ------------------

# The EXACT final-result snippet from the real no-land that prompted this branch:
# .loop-runs/run-20260625T201454Z-18801.jsonl pass [21] (L1382 "Conviction Queue thesis-met
# bubble-up"). The agent found the item's premise debatable, tried AskUserQuestion (unavailable under
# headless `claude -p`), and surfaced the fork in prose instead of guessing — a CLEAN tree, no commit,
# and NOT a background-job yield. The oracle is this externally-observed result, not anything the
# classifier itself produces. Proven to read 'no-land' on the old logic and 'blocked' on the new.
_PASS21_FORK_SNIPPET = (
    "`AskUserQuestion` isn't available here, so I'll surface this in prose. This is a genuine fork "
    "worth your call before I build, because the item's premise was …"
)


def test_real_pass21_clean_tree_decision_fork_is_blocked_not_no_land() -> None:
    """REGRESSION: the real 8th-occurrence no-land (clean tree, agent escalated a decision fork it
    can't resolve headless) must classify as 'blocked', not collapse into a generic 'no-land' that
    reads as a stuck item rather than an escalation."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS21_FORK_SNIPPET},
    }
    assert _pass_outcome(rec) == "blocked"


def test_self_resolved_deferral_stays_no_land_not_blocked() -> None:
    """A clean-tree no-land where the agent handled the deferral ITSELF (parked / Depends-on, no human
    call needed) stays 'no-land' — only an escalation that asks for a human decision is 'blocked'."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": "The top item is a Track-A subsystem needing a shaped design first; "
            "parking it trigger-gated and working the next ready item instead.",
        },
    }
    assert _pass_outcome(rec) == "no-land"


# The EXACT final-result snippet from run-20260630T204420Z-25533.jsonl pass [8] (L2904
# "Quality-first discovery lane" — a Track-A NEW SUBSYSTEM the headless drainer picked because its
# only dependency was met). The agent researched, hit the shape-first design fork, and PRESENTED
# build options instead of building — never uttering "AskUserQuestion" / "genuine fork" / "Track A
# fork", so the prior marker set missed it, terminal_signal recorded null, and the pass read a
# generic `no-land` (the loop printed the misleading "inspect, then re-run"). The oracle is this
# externally-observed result; proven to read 'no-land' on the old markers and 'blocked' on the new
# "build option" family. (project_loop_next_todo_no_land_root_causes, 13th occurrence 2026-06-30.)
_PASS8_SHAPE_FIRST_SNIPPET = (
    "Research done. Here's the shape of the codebase this needs to fit into, then two "
    "concrete build options.  **What already exists (no new infra needed for most…"
)


def test_real_pass8_track_a_shape_first_is_blocked_not_no_land() -> None:
    """REGRESSION (13th occurrence): a Track-A shape-first escalation that PRESENTS build options
    instead of building must classify 'blocked', not a generic 'no-land'. terminal_signal is null on
    the real record (the phrasing missed the full-text classifier too), so this exercises the
    snippet fallback in _pass_outcome — the same path the real spin-guard read via `last-outcome`."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": _PASS8_SHAPE_FIRST_SNIPPET,
            "terminal_signal": None,
        },
    }
    assert _pass_outcome(rec) == "blocked"


# The EXACT final-result snippet from run-20260701T041052Z-76457.jsonl pass [5] (L2909 "Multi-factor
# discovery sort" — a Track-A SHAPE-FIRST item the headless drainer picked). The agent researched,
# surfaced the load-bearing architecture constraint (yfinance screen_equities() sorts on ONE scalar
# field), and REPORTED THE CONCLUSION instead of building — never uttering "AskUserQuestion" /
# "genuine fork" / "Track A fork" / "build option", so the prior marker set missed it,
# terminal_signal recorded null, and the pass read a generic `no-land` (the loop printed the
# misleading "inspect, then re-run"). The oracle is this externally-observed result; proven to read
# 'no-land' on the old markers and 'blocked' on the new "confirmed the architecture" marker.
# (project_loop_next_todo_no_land_root_causes, 14th occurrence 2026-07-01.)
_PASS5_ARCH_CONCLUSION_SNIPPET = (
    "I've confirmed the architecture facts. Here's the key finding: **yfinance's "
    "`screen_equities()` sort only accepts a fixed scalar field name — it cannot sort …"
)


def test_real_pass5_track_a_arch_conclusion_is_blocked_not_no_land() -> None:
    """REGRESSION (14th occurrence): a Track-A shape-first pass that REPORTS A RESEARCH CONCLUSION
    (a blocking architecture constraint) instead of building must classify 'blocked', not a generic
    'no-land'. terminal_signal is null on the real record (the phrasing missed the full-text
    classifier too), so this exercises the snippet fallback in _pass_outcome — the same path the
    real spin-guard read via `last-outcome` when it printed the misleading 'inspect, then re-run'."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": _PASS5_ARCH_CONCLUSION_SNIPPET,
            "terminal_signal": None,
        },
    }
    assert _pass_outcome(rec) == "blocked"


# The real final result text from run-20260702T205545Z-17427.jsonl pass [3] (L3592 selector pick;
# the agent foundation-first-overrode to L3611 "Derivation-map lineage API" — a Track-A new subsystem —
# then ended on a LETTERED OPTION MENU + "reply go / unless you redirect"). It matched ZERO of the
# _DECISION_FORK_MARKERS ("Confirmed the design surface" ≠ "confirmed the architecture"; "The one
# genuine open decision" ≠ "genuine fork"; no "build option" / "Track A fork" wording), so the record
# persisted terminal_signal=null, the pass read a generic `no-land`, and the loop printed the
# misleading "inspect, then re-run" (a re-run spin-stops the same way). The STRUCTURAL detector
# (_looks_like_option_menu_escalation) is what catches it now — vocabulary-independent, so it won't
# need a 20th marker for the next escalation's wording. The oracle is this externally-observed real
# output. (project_loop_next_todo_no_land_root_causes, 16th occurrence 2026-07-02.)
_PASS3_OPTION_MENU_FULL_RESULT = (
    "Confirmed the design surface. Here's where I've landed.\n\n"
    "## Pick: P1 L3611 — Derivation-map lineage API (Audit-Mode foundation)\n\n"
    "**Why this over the selector's default (L3592 coverage manifest):** foundation-first override. "
    "The default is a near-zero-cost leaf that unblocks nothing. L3611 is the keystone of the "
    "derivation-map spine, unblocking **3 downstream P2s** — all `Depends on:` it. Build the keystone "
    "first.\n\nThis is **Track A** — it introduces a new derivation-map response shape 3 consumers "
    "will depend on, with a subtle load-bearing correctness contract. Per the skill, that earns a "
    "design confirmation before a multi-hour build.\n\n"
    "**The one genuine open decision — scope of this pass.**\n\n"
    "- **(A) Foundation API only, full FIXED formula set** *(my recommendation)* — the ticket as written.\n"
    "- **(B) Foundation API only, load-bearing slice first** — just FCF + FCF-ROE aggregate.\n"
    "- **(C) Foundation + first consumer as one vertical** — pair it with the owner-earnings waterfall.\n\n"
    "I'll proceed with **(A)** unless you redirect. Reply \"go\" (or A/B/C) and I'll `/goalify` it "
    "and build end-to-end."
)


def test_real_pass3_track_a_option_menu_is_blocked_via_full_text() -> None:
    """REGRESSION (16th occurrence): a Track-A shape-first escalation that ends on a LETTERED OPTION
    MENU + a solicited go/no-go must classify 'blocked', not a generic 'no-land'. It matches NO exact
    marker, so this exercises the STRUCTURAL detector via the record-time full-text path (_norm_claude
    → _classify_terminal_signal), which is where a live pass gets classified — the truncated snippet
    the loop's spin-guard reads via `last-outcome` would miss the menu, so the fix must land at record
    time on the full text (the same structural fix as the 10th-occurrence truncation blind spot)."""
    n = _norm_claude({**_RESULT, "result": _PASS3_OPTION_MENU_FULL_RESULT})
    assert n["terminal_signal"] == "decision_fork"
    rec = {"rc": 0, "landed": False, "dirty_count": 0, "claude": n}
    assert _pass_outcome(rec) == "blocked"


def test_bg_yield_takes_precedence_over_fork_when_both_match() -> None:
    """A bg-yield snippet classifies 'stalled' even if a fork phrase also appears — stalled is checked
    first, and re-run (not go-decide) is the correct next move for an abandoned background job."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": "Kicked off the probe running in the background; this is a genuine "
            "fork but I'll await its completion notification.",
        },
    }
    assert _pass_outcome(rec) == "stalled"


def test_dirty_tree_beats_fork_snippet() -> None:
    """A decision-fork snippet WITH a dirty tree is 'abandoned' — the dirty-tree branch wins so the
    work-recovery prompt always fires (recoverable work outranks a display label)."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 3,
        "claude": {"is_error": False, "result_snippet": _PASS21_FORK_SNIPPET},
    }
    assert _pass_outcome(rec) == "abandoned"


def test_landed_beats_fork_snippet() -> None:
    """A landed pass is 'landed' even if its result text happens to mention a fork / AskUserQuestion."""
    rec = {
        "rc": 0,
        "landed": True,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS21_FORK_SNIPPET},
    }
    assert _pass_outcome(rec) == "landed"


@pytest.mark.parametrize(
    "snippet",
    [
        _PASS21_FORK_SNIPPET,
        "AskUserQuestion isn't available, so surfacing in prose.",
        "This is a genuine fork worth your call before I build.",
        "The premise needs your decision before I can proceed.",
        # The real run-20260630T123905Z L2125 escalation: a `/next-todo` Track-A
        # ("new subsystem → shape first") fork the pass mis-escalated instead of
        # parking. "genuine fork" does NOT match "genuine Track A fork", so before the
        # Track-A markers this read `no-land` and the loop printed the misleading
        # "inspect, then re-run" rather than the `blocked` "GO DECIDE, then park".
        "The selector's top READY pick is L2125 — but it's a genuine Track A fork, "
        "so I'm pausing here.",
        "This is a new subsystem; I'll shape it first before building.",
        # The real run-20260630T204420Z-25533 pass [8] shape-first escalation: it PRESENTS build
        # options rather than uttering any fork/AskUserQuestion wording, so the "build option"
        # family is what catches it (13th occurrence 2026-06-30).
        _PASS8_SHAPE_FIRST_SNIPPET,
        "Here are two options for the caching layer; which do you want?",
        # The real run-20260701T041052Z-76457 pass [5] arch-conclusion escalation: it REPORTS a
        # blocking architecture constraint from research rather than any fork/options wording, so the
        # "confirmed the architecture" marker is what catches it (14th occurrence 2026-07-01).
        _PASS5_ARCH_CONCLUSION_SNIPPET,
        "This is an architectural constraint that needs a design call before I build.",
    ],
)
def test_looks_like_decision_fork_matches_escalation_phrasing(snippet: str) -> None:
    assert _looks_like_decision_fork(snippet) is True


@pytest.mark.parametrize(
    "snippet",
    [
        None,
        "",
        "Shipped the fix and pushed to main.",
        "Item is blocked; deferred with a Depends-on line.",
        "Parking it trigger-gated and moving to the next item.",
        _PASS2_STALL_SNIPPET,  # a bg-yield is NOT a decision fork
    ],
)
def test_looks_like_decision_fork_ignores_non_escalation_text(snippet: str | None) -> None:
    assert _looks_like_decision_fork(snippet) is False


# --- structural (vocabulary-independent) option-menu / solicited-go-no-go escalation ---------------


@pytest.mark.parametrize(
    "text",
    [
        # A lettered OPTION MENU presented at the END of the turn, with NONE of the exact markers.
        "The shape is dictated by the code. Scope decision for this pass:\n"
        "(A) full formula set\n(B) load-bearing slice\n(C) foundation + first consumer.\n"
        "Proceeding with (A).",
        # A solicited go/no-go tail with NO lettered menu and NO exact marker.
        "The design is shaped and the endpoint is decided. Shall I proceed with the build?",
        # The real occurrence-16 shape (matches via BOTH the menu and the solicit tail).
        _PASS3_OPTION_MENU_FULL_RESULT,
    ],
)
def test_option_menu_escalation_matches_structural_shapes(text: str) -> None:
    assert _looks_like_option_menu_escalation(text) is True
    # The public entry point agrees, reached via the structural fallback (no exact marker present).
    assert _looks_like_decision_fork(text) is True


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Shipped the fix and pushed to main.",
        # A self-resolved park — the agent handled the deferral itself; no human go/no-go solicited.
        "The top item is a Track-A subsystem; parking it trigger-gated and working the next item.",
        # A SINGLE incidental "(a)" in a mid-analysis aside is not a menu, and there is no solicit tail.
        "The FX gate (a) already guards this path, so nothing to change this pass.",
        _PASS2_STALL_SNIPPET,  # a bg-yield tail is neither a menu nor a solicitation
    ],
)
def test_option_menu_escalation_ignores_non_escalation(text: str | None) -> None:
    assert _looks_like_option_menu_escalation(text) is False


def test_blocked_note_only_for_blocked_clean_pass() -> None:
    blocked = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS21_FORK_SNIPPET},
    }
    note = _blocked_note(blocked)
    assert note is not None
    assert "decision fork" in note
    # Not for landed, dirty (abandoned), stalled, or genuine no-land passes.
    assert _blocked_note({**blocked, "landed": True}) is None
    assert _blocked_note({**blocked, "dirty_count": 5}) is None
    assert (
        _blocked_note(
            {
                "rc": 0,
                "landed": False,
                "dirty_count": 0,
                "claude": {"result_snippet": _PASS2_STALL_SNIPPET},
            }
        )
        is None
    )


def test_blocked_note_quotes_terminal_excerpt_when_present() -> None:
    """THE FIX (L2887): the note now quotes the actual fork the agent escalated, closing the exact
    gap the L53 pass hit — the loop deleted the pass log and only a 157-char snippet survived, so
    the operator couldn't see WHAT decision was escalated without re-running interactively."""
    excerpt = (
        "AskUserQuestion isn't available here, so I'll surface this in prose: the metric picker "
        "needs a direction/sane-range hint scheme — pick per-metric config vs a generic clamp."
    )
    blocked = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "result_snippet": _PASS21_FORK_SNIPPET,
            "terminal_excerpt": excerpt,
        },
    }
    note = _blocked_note(blocked)
    assert note is not None
    assert excerpt in note


def test_blocked_note_falls_back_to_generic_when_excerpt_absent() -> None:
    """A legacy record (predates `terminal_excerpt`) still gets the generic go-decide prompt — no
    crash, no dangling ': "None"' in the note."""
    legacy = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _PASS21_FORK_SNIPPET},
    }
    note = _blocked_note(legacy)
    assert note is not None
    assert "None" not in note
    assert note.endswith("spin-stop here again")


# --- terminal_signal: classify from FULL text, not the truncated snippet --------

# The EXACT truncated snippet the loop persisted for the real 10th-occurrence no-land
# (.loop-runs/run-20260627T175442Z-73183.jsonl pass [1], L53 "Kill-criteria arming UI").
# The agent escalated a decision fork, but `_norm_claude` truncates the result to 157
# chars + "…" and the fork markers sit PAST the boundary — so a scan of THIS snippet finds
# nothing and (under the old logic) mislabels the pass `no-land`. Externally observed.
_L53_TRUNCATED_SNIPPET = (
    "I've finished the investigation. Here's a decisive finding that changes "
    "L53's scope, so I'm surfacing it before building (Track A's \"confirm "
    "before a multi-h…"
)

# A plausible FULL result for that pass: the same long lead, with the decision-fork markers
# ("AskUserQuestion", "surface this in prose", "genuine fork", "worth your call") landing
# only after the 157-char truncation point — the shape that defeated the snippet scan.
_L53_FULL_FORK_RESULT = (
    "I've finished the investigation. Here's a decisive finding that changes "
    "L53's scope, so I'm surfacing it before building (Track A's \"confirm before "
    'a multi-hour build"): the metric catalog the picker needs already exists, so '
    "the scope is smaller than the ticket implies. AskUserQuestion isn't available "
    "under headless `claude -p`, so I'll surface this in prose — it's a genuine fork "
    "worth your call before I build."
)

# A FULL result whose background-job-yield marker likewise falls past the truncation point.
_LONG_BG_YIELD_RESULT = (
    "Kicked off the cohort backfill across all 24 asset-light names to gather the "
    "goodwill and SBC rows the larger validation needs; that is the slow part and it "
    "is now running. I'll await its completion notification rather than poll, then "
    "score the expanded set."
)


def test_real_l53_truncated_snippet_loses_the_fork_marker() -> None:
    """REGRESSION (the defect): the real L53 truncated snippet carries NO decision-fork
    marker — they were cut by the 157-char truncation — so the snippet scan that the old
    `_pass_outcome` relied on mislabels a genuine escalation as a plain `no-land`."""
    assert len(_L53_TRUNCATED_SNIPPET) <= 160
    assert _looks_like_decision_fork(_L53_TRUNCATED_SNIPPET) is False
    legacy = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {"is_error": False, "result_snippet": _L53_TRUNCATED_SNIPPET},
    }
    # No terminal_signal + a marker-less snippet → the best the loop can do on a legacy
    # record is `no-land`. The fix below makes new records classify from the full text.
    assert _pass_outcome(legacy) == "no-land"


def test_decision_fork_past_snippet_truncation_classified_via_full_text() -> None:
    """THE FIX: `_norm_claude` classifies terminal intent from the FULL result text before
    truncating, so a decision-fork whose markers fall past char 157 is still scored
    `blocked` — not the generic `no-land` the truncated-snippet scan would yield."""
    norm = _norm_claude({"result": _L53_FULL_FORK_RESULT, "subtype": "success", "is_error": False})
    # The display snippet was truncated, so the markers are GONE from it (the blind spot)…
    assert "askuserquestion" not in (norm["result_snippet"] or "").lower()
    assert _looks_like_decision_fork(norm["result_snippet"]) is False
    # …but the persisted signal, computed from the full text, captured the escalation.
    assert norm["terminal_signal"] == "decision_fork"
    rec = {"rc": 0, "landed": False, "dirty_count": 0, "claude": norm}
    assert _pass_outcome(rec) == "blocked"


def test_bg_yield_past_snippet_truncation_classified_via_full_text() -> None:
    """Symmetric fix for the `stalled` class: a background-job yield whose marker falls past
    the truncation point is still classified from the full text."""
    norm = _norm_claude({"result": _LONG_BG_YIELD_RESULT, "subtype": "success", "is_error": False})
    assert norm["terminal_signal"] == "bg_yield"
    rec = {"rc": 0, "landed": False, "dirty_count": 0, "claude": norm}
    assert _pass_outcome(rec) == "stalled"


def test_classify_terminal_signal_precedence_and_none() -> None:
    """bg_yield is checked before decision_fork (re-run before go-decide); non-str / no-marker
    text returns None."""
    both = (
        "This is a genuine fork worth your call, but the probe is running in the "
        "background and I'll await its completion notification."
    )
    assert _classify_terminal_signal(both) == "bg_yield"
    assert _classify_terminal_signal("Shipped the fix and pushed to main.") is None
    assert _classify_terminal_signal(None) is None
    assert _classify_terminal_signal("") is None


def test_legacy_record_without_terminal_signal_falls_back_to_snippet() -> None:
    """A record predating the field (no `terminal_signal` key) still classifies via the
    truncated-snippet scan — old behaviour preserved. An explicit None also falls back."""
    base = {"rc": 0, "landed": False, "dirty_count": 0}
    # Legacy: key absent, but the marker survived in the (short) snippet.
    assert (
        _pass_outcome(
            {**base, "claude": {"is_error": False, "result_snippet": _PASS21_FORK_SNIPPET}}
        )
        == "blocked"
    )
    # Explicit None signal → same fallback path.
    assert (
        _pass_outcome(
            {
                **base,
                "claude": {
                    "is_error": False,
                    "terminal_signal": None,
                    "result_snippet": _PASS21_FORK_SNIPPET,
                },
            }
        )
        == "blocked"
    )


def test_terminal_signal_is_authoritative_over_snippet() -> None:
    """When present, the persisted signal wins over the snippet — a record whose snippet was
    truncated marker-free but whose full-text classification found the fork reads `blocked`."""
    rec = {
        "rc": 0,
        "landed": False,
        "dirty_count": 0,
        "claude": {
            "is_error": False,
            "terminal_signal": "decision_fork",
            "result_snippet": _L53_TRUNCATED_SNIPPET,  # marker-free
        },
    }
    assert _pass_outcome(rec) == "blocked"


def test_norm_claude_persists_terminal_signal_none_for_benign_result() -> None:
    """A landed/benign result carries no terminal signal; the field is present and None."""
    norm = _norm_claude({"result": "Shipped v0.36.x and pushed to main.", "is_error": False})
    assert norm["terminal_signal"] is None
    assert norm["result_snippet"] == "Shipped v0.36.x and pushed to main."


# --- summary -----------------------------------------------------------------


def _rec(**kw: object) -> dict:
    base = {
        "iter": 1,
        "pick": {"priority": "P1", "title": "Item", "line": 1},
        "rc": 0,
        "wall_s": 60,
        "closed_before": 10,
        "closed_after": 11,
        "landed": True,
        "new_prs": [],
        "claude": {
            "parsed": True,
            "subtype": "success",
            "is_error": False,
            "num_turns": 5,
            "cost_usd": 1.0,
            "tokens": {"input": 1000, "output": 100, "cache_read": 5000, "cache_creation": 200},
            "permission_denials": 0,
        },
    }
    base.update(kw)
    return base


def test_summary_aggregates_totals() -> None:
    recs = [
        _rec(iter=1, wall_s=60, claude={**_rec()["claude"], "cost_usd": 1.0, "num_turns": 5}),
        _rec(
            iter=2,
            wall_s=120,
            landed=False,
            closed_before=11,
            closed_after=11,
            claude={**_rec()["claude"], "cost_usd": 2.0, "num_turns": 10},
        ),
    ]
    out = render_summary(recs, reason="drained", ready_remaining="3")
    assert "passes: 2" in out
    assert "landed: 1" in out
    assert "no-landing: 1" in out
    assert "$3.00 total" in out  # 1.0 + 2.0
    assert "15 total" in out  # turns 5 + 10
    assert "ready remaining: 3" in out
    assert "closed 10→11" in out  # first closed_before → last closed_after


def test_summary_flags_permission_denials() -> None:
    recs = [_rec(claude={**_rec()["claude"], "permission_denials": 3})]
    out = render_summary(recs)
    assert "permission denials: 3" in out
    assert "allowlist wall" in out


def test_summary_counts_merged_prs() -> None:
    recs = [
        _rec(new_prs=[{"number": 225, "state": "MERGED"}]),
        _rec(iter=2, new_prs=[{"number": 226, "state": "OPEN"}]),
    ]
    out = render_summary(recs)
    assert "created 2" in out
    assert "merged 1" in out


def test_summary_handles_missing_cost() -> None:
    """A pass whose claude result didn't parse has no cost — summary must not crash."""
    recs = [_rec(claude={"parsed": False})]
    out = render_summary(recs)
    assert "LOOP SUMMARY" in out  # no exception; cost line simply omitted


def test_summary_surfaces_unmerged_work_on_reused_branch() -> None:
    """A no-land pass that left an OPEN PR on a reused claude/* branch is flagged, not silently lost."""
    recs = [
        _rec(
            iter=1,
            landed=False,
            closed_before=10,
            closed_after=10,
            branch_end="claude/foo-123",
            branch_commits_ahead=3,
            branch_open_pr={"number": 226, "state": "OPEN"},
        )
    ]
    out = render_summary(recs)
    assert "in-flight / unmerged work" in out
    assert "PR #226 OPEN" in out
    assert "claude/foo-123" in out


def test_summary_no_unmerged_block_when_all_landed_on_main() -> None:
    """The default landed-on-main passes produce no unmerged-work block."""
    out = render_summary([_rec()])
    assert "in-flight / unmerged work" not in out


def test_summary_surfaces_abandoned_work() -> None:
    """An abandoned-mid-land pass is counted + surfaced as recoverable, not silently 'no-land'."""
    recs = [
        _rec(
            iter=1,
            landed=False,
            closed_before=10,
            closed_after=10,
            dirty_count=8,
        )
    ]
    out = render_summary(recs)
    assert "abandoned: 1" in out
    assert "abandoned mid-land" in out
    assert "8 uncommitted file(s)" in out
    assert "RECOVERABLE" in out


def test_summary_no_abandoned_count_when_none_abandoned() -> None:
    """The abandoned count is omitted from the counts line when no pass abandoned work."""
    out = render_summary([_rec()])
    assert "abandoned:" not in out
    assert "abandoned mid-land" not in out


def test_summary_surfaces_stalled_pass() -> None:
    """A clean-tree background-job yield is counted + surfaced as stalled, not a generic no-land."""
    recs = [
        _rec(
            iter=1,
            landed=False,
            closed_before=10,
            closed_after=10,
            dirty_count=0,
            claude={**_rec()["claude"], "result_snippet": _PASS2_STALL_SNIPPET},
        )
    ]
    out = render_summary(recs)
    assert "stalled: 1" in out
    assert "background-job callback" in out
    # A stall is no progress: it must NOT be counted as a landing.
    assert "landed: 0" in out


def test_summary_no_stalled_block_when_none_stalled() -> None:
    """The stalled count + block are omitted when no pass stalled."""
    out = render_summary([_rec()])
    assert "stalled:" not in out
    assert "stalled (agent yielded" not in out


def test_summary_surfaces_blocked_pass() -> None:
    """A clean-tree decision-fork escalation is counted + surfaced as blocked with a go-decide
    prompt, not a generic no-land that reads as a stuck item."""
    recs = [
        _rec(
            iter=1,
            landed=False,
            closed_before=10,
            closed_after=10,
            dirty_count=0,
            claude={**_rec()["claude"], "result_snippet": _PASS21_FORK_SNIPPET},
        )
    ]
    out = render_summary(recs)
    assert "blocked: 1" in out
    assert "decision fork" in out
    assert "GO DECIDE" in out
    # An escalation is no progress: it must NOT be counted as a landing.
    assert "landed: 0" in out


def test_summary_no_blocked_block_when_none_blocked() -> None:
    """The blocked count + block are omitted when no pass escalated a decision fork."""
    out = render_summary([_rec()])
    assert "blocked:" not in out
    assert "blocked (agent escalated" not in out


def test_summary_blocked_block_quotes_terminal_excerpt() -> None:
    """End-to-end: a blocked pass with a persisted `terminal_excerpt` surfaces the ACTUAL fork in
    the end-of-run summary block, not just the generic go-decide prompt — the L2887 fix, exercised
    through the same `render_summary` path the loop's real STOP message reads."""
    excerpt = "Two build options: a cached watchlist read-model, or a full new discovery lane."
    recs = [
        _rec(
            iter=8,
            landed=False,
            closed_before=10,
            closed_after=10,
            dirty_count=0,
            claude={
                **_rec()["claude"],
                "result_snippet": _PASS21_FORK_SNIPPET,
                "terminal_excerpt": excerpt,
            },
        )
    ]
    out = render_summary(recs)
    assert "blocked: 1" in out
    assert excerpt in out


def test_summary_counts_unknown_pass() -> None:
    """A pass where the gh check failed AND origin/main was unresolvable is counted as `unknown`,
    not folded into no-landing — and the PR column reads "unknown (gh unavailable)"."""
    recs = [
        _rec(
            iter=1,
            landed=False,
            head_advanced=None,
            pr_check_failed=True,
            new_prs=[],
            closed_before=10,
            closed_after=10,
            dirty_count=0,
        )
    ]
    out = render_summary(recs)
    assert "unknown: 1" in out
    assert "no-landing: 0" in out
    assert "landed: 0" in out
    assert "unknown (gh unavailable)" in out


def test_summary_no_unknown_count_when_none_unknown() -> None:
    """The unknown count is omitted when no pass was indeterminate."""
    out = render_summary([_rec()])
    assert "unknown:" not in out


def test_summary_cost_avg_over_reported_passes_not_all() -> None:
    """Cost averages over passes that reported a cost, not all n — an unparsed pass (no cost)
    must not deflate the mean. Two passes, one $2.00, one unparsed → avg is $2.00, not $1.00."""
    recs = [
        _rec(iter=1, claude={**_rec()["claude"], "cost_usd": 2.0}),
        _rec(iter=2, claude={"parsed": False}),
    ]
    out = render_summary(recs)
    assert "$2.00 total" in out
    assert "$2.00 avg/reported-pass" in out


def test_summary_empty_records() -> None:
    out = render_summary([], reason="nothing ran")
    assert "no passes recorded" in out


def test_summary_error_pass_counted() -> None:
    recs = [_rec(rc=1, claude={**_rec()["claude"], "is_error": True})]
    out = render_summary(recs)
    assert "errored: 1" in out


# --- cli wiring --------------------------------------------------------------


def test_main_record_then_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rf = tmp_path / "run.jsonl"
    resf = tmp_path / "res.json"
    resf.write_text(json.dumps(_RESULT), encoding="utf-8")
    rc = main(
        [
            "record",
            "--run-file",
            str(rf),
            "--iter",
            "1",
            "--priority",
            "P1",
            "--title",
            "T",
            "--line",
            "10",
            "--rc",
            "0",
            "--started",
            "1000",
            "--ended",
            "1100",
            "--closed-before",
            "5",
            "--closed-after",
            "6",
            "--result-file",
            str(resf),
            "--new-prs-json",
            json.dumps([{"number": 225, "state": "MERGED"}]),
        ]
    )
    assert rc == 0
    line = capsys.readouterr().out
    assert "landed" in line

    rc = main(["summary", "--run-file", str(rf), "--reason", "drained"])
    assert rc == 0
    assert "passes: 1" in capsys.readouterr().out


# --- last-outcome: the spin-guard's tailored-stop-message input -----------------
# The bash spin-guard reads `loop_stats.py last-outcome` to decide whether a re-pick
# after a no-land should advise a plain "re-run" (generic no-land) or "run synchronously /
# park the item" (a `stalled` bg-yield or `blocked` decision-fork last pass — a plain re-run
# reproduces the same stop). Reuses _pass_outcome, so the stop line can't disagree with the
# per-pass line + summary (project_loop_next_todo_no_land_root_causes, 11th occurrence).


def test_last_outcome_empty_run_file_prints_blank(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"  # never created → no records
    rc = main(["last-outcome", "--run-file", str(rf)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_last_outcome_reads_the_LAST_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A multi-pass run: last-outcome reflects only the final record (a landed pass here)."""
    rf = tmp_path / "run.jsonl"
    cmd_record(_Args(run_file=str(rf), iter=1, closed_before=40, closed_after=40))  # no-land
    cmd_record(_Args(run_file=str(rf), iter=2, closed_before=40, closed_after=41))  # landed
    capsys.readouterr()  # drain the per-pass lines cmd_record printed
    rc = main(["last-outcome", "--run-file", str(rf)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "landed"


def test_last_outcome_stalled_on_clean_tree_bg_yield(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 11th-occurrence pass: a clean-tree, no-land bg-yield must read `stalled` here so the
    spin-guard prints the run-synchronously/park message instead of the blind "re-run"."""
    rf = tmp_path / "run.jsonl"
    resf = tmp_path / "res.json"
    resf.write_text(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "I'll stop here and wait for the reextract completion notification.",
            }
        ),
        encoding="utf-8",
    )
    cmd_record(
        _Args(
            run_file=str(rf),
            result_file=str(resf),
            closed_before=138,
            closed_after=138,  # no land
            dirty_count=0,  # clean tree → not `abandoned`
            head_advanced="0",
        )
    )
    rec = _read_one(rf)
    assert rec["claude"]["terminal_signal"] == "bg_yield"  # full-text classification at record time
    assert _pass_outcome(rec) == "stalled"
    capsys.readouterr()  # drain the per-pass line cmd_record printed
    rc = main(["last-outcome", "--run-file", str(rf)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "stalled"


# --- budget-check (run-level advisory cap) -----------------------------------
# Expected values are spec-derived (the LOOP_MAX_RUN_BUDGET_USD contract in loop_next_todo.sh's
# header): exit 3 strictly-over, exit 0 at-or-under / fail-soft, stdout = 4-decimal total.


def _write_pass_records(rf: Path, costs: list[object]) -> None:
    with rf.open("a", encoding="utf-8") as fh:
        for c in costs:
            fh.write(
                json.dumps({"iter": 1, "rc": 0, "claude": {"parsed": True, "cost_usd": c}}) + "\n"
            )


def test_budget_check_under_budget_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"
    _write_pass_records(rf, [10.0, 12.5])
    rc = main(["budget-check", "--run-file", str(rf), "--max-usd", "37.50"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "22.5000"


def test_budget_check_over_budget_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"
    _write_pass_records(rf, [20.0, 18.0])
    rc = main(["budget-check", "--run-file", str(rf), "--max-usd", "37.50"])
    assert rc == 3
    assert capsys.readouterr().out.strip() == "38.0000"


def test_budget_check_at_exactly_max_is_not_over(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"
    _write_pass_records(rf, [37.5])
    rc = main(["budget-check", "--run-file", str(rf), "--max-usd", "37.50"])
    assert rc == 0
    capsys.readouterr()


def test_budget_check_missing_file_fails_soft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["budget-check", "--run-file", str(tmp_path / "absent.jsonl"), "--max-usd", "1"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "0.0000"


def test_budget_check_skips_none_and_malformed_costs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"
    # None (crashed pass), a string (corrupt record), and a bool must all be excluded.
    _write_pass_records(rf, [None, "12.0", True, 5.0])
    with rf.open("a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
    rc = main(["budget-check", "--run-file", str(rf), "--max-usd", "100"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "5.0000"


def test_budget_check_unparseable_max_fails_soft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"
    _write_pass_records(rf, [50.0])
    rc = main(["budget-check", "--run-file", str(rf), "--max-usd", "not-a-number"])
    assert rc == 0
    capsys.readouterr()


def test_budget_check_excludes_autopark_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rf = tmp_path / "run.jsonl"
    _write_pass_records(rf, [30.0])
    with rf.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "autopark", "claude": {"cost_usd": 99.0}}) + "\n")
    rc = main(["budget-check", "--run-file", str(rf), "--max-usd", "40"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "30.0000"


# --- Gate: commit-trailer verdict (gate-fail outcome) --------------------------
# Spec-derived: gate_pass is tri-state — None (no trailer; gate:none, NOT a failure),
# True (green), False (red → outcome `gate-fail`, landings only).


def test_record_gate_red_on_landing_is_gate_fail(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(
            run_file=str(rf),
            closed_before=1,
            closed_after=1,
            head_advanced="1",  # landed
            gate_pass="0",
            gate_cmd="uv run pytest tests/filter/test_x.py -q",
        )
    )
    rec = _read_one(rf)
    assert rec["landed"] is True
    assert rec["gate_pass"] is False
    assert rec["gate_cmd"] == "uv run pytest tests/filter/test_x.py -q"
    assert _pass_outcome(rec) == "gate-fail"


def test_record_gate_green_on_landing_stays_landed(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(run_file=str(rf), closed_before=1, closed_after=1, head_advanced="1", gate_pass="1")
    )
    rec = _read_one(rf)
    assert rec["gate_pass"] is True
    assert _pass_outcome(rec) == "landed"


def test_record_no_gate_trailer_is_gate_none_not_failure(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(run_file=str(rf), closed_before=1, closed_after=1, head_advanced="1", gate_pass="")
    )
    rec = _read_one(rf)
    assert rec["gate_pass"] is None
    assert rec["gate_cmd"] is None
    assert _pass_outcome(rec) == "landed"


def test_gate_red_without_landing_does_not_gate_fail(tmp_path: Path) -> None:
    # The gate verdict only qualifies LANDINGS — a no-land pass keeps its own outcome even if a
    # stray gate value was recorded (the shell never runs gates on a no-land, so this is a guard).
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(run_file=str(rf), closed_before=1, closed_after=1, head_advanced="0", gate_pass="0")
    )
    rec = _read_one(rf)
    assert _pass_outcome(rec) == "no-land"


def test_legacy_record_without_gate_fields_is_unchanged() -> None:
    assert _pass_outcome({"rc": 0, "landed": True}) == "landed"


def test_summary_gates_line_counts_green_red_none(tmp_path: Path) -> None:
    recs = [
        {"iter": 1, "rc": 0, "landed": True, "gate_pass": True, "claude": {}},
        {"iter": 2, "rc": 0, "landed": True, "gate_pass": False, "gate_cmd": "make x", "claude": {}},
        {"iter": 3, "rc": 0, "landed": True, "gate_pass": None, "claude": {}},
        {"iter": 4, "rc": 0, "landed": False, "claude": {}},  # non-landing: excluded from gates
    ]
    text = render_summary(recs, reason="drained")
    assert "gate-fail: 1" in text
    assert "gates: 1 green · 1 red · 1 none — of 3 landing(s)" in text


def test_summary_no_gates_line_without_landings() -> None:
    text = render_summary([{"iter": 1, "rc": 0, "landed": False, "claude": {}}], reason="x")
    assert "gates:" not in text


def test_render_pass_line_gate_fail_names_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rf = tmp_path / "run.jsonl"
    cmd_record(
        _Args(
            run_file=str(rf),
            closed_before=1,
            closed_after=1,
            head_advanced="1",
            gate_pass="0",
            gate_cmd="make check",
        )
    )
    out = capsys.readouterr().out
    assert "gate-fail" in out
    assert "make check" in out


# --- history (cross-run aggregation) -------------------------------------------
# Spec-derived: offenders = >=2 stalled/blocked/no-land/abandoned passes or >=2 autoparks;
# ERROR/unknown excluded (retry inflation); most-recent-landed => "(resolved)" tag kept.


def _write_run(stats_dir: Path, name: str, records: list[dict]) -> None:
    stats_dir.mkdir(parents=True, exist_ok=True)
    with (stats_dir / name).open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _pass(title: str, *, landed: bool = False, rc: int = 0, cost: float | None = None) -> dict:
    return {
        "iter": 1,
        "rc": rc,
        "landed": landed,
        "dirty_count": 0,
        "head_advanced": landed or None,
        "pick": {"title": title},
        "claude": {"parsed": True, "cost_usd": cost},
    }


def test_history_empty_dir_fails_soft(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["history", "--stats-dir", str(tmp_path / "nowhere")])
    assert rc == 0
    assert "no run-*.jsonl" in capsys.readouterr().out


def test_history_totals_and_repeat_offenders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "runs"
    _write_run(d, "run-20260701T000000Z-1.jsonl", [_pass("flaky item"), _pass("ok item", landed=True, cost=2.0)])
    _write_run(d, "run-20260702T000000Z-2.jsonl", [_pass("flaky item"), _pass("one-off miss")])
    rc = main(["history", "--stats-dir", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 run(s)" in out
    assert "no-land 3" in out and "landed 1" in out
    assert "flaky item — 2 non-landing pass(es)" in out
    assert "one-off miss" not in out.split("repeat offenders")[1]  # single miss is not an offender


def test_history_excludes_error_passes_from_offenders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "runs"
    # 3 consecutive transient ERRORs on the same pick (the retry arm re-records the same title).
    _write_run(d, "run-20260701T000000Z-1.jsonl", [_pass("outage victim", rc=1) for _ in range(3)])
    rc = main(["history", "--stats-dir", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ERROR 3" in out
    assert "repeat offenders" not in out


def test_history_resolved_tag_when_most_recent_landed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "runs"
    _write_run(d, "run-20260701T000000Z-1.jsonl", [_pass("eventually fixed"), _pass("eventually fixed")])
    _write_run(d, "run-20260702T000000Z-2.jsonl", [_pass("eventually fixed", landed=True)])
    rc = main(["history", "--stats-dir", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "eventually fixed" in out
    assert "(resolved)" in out


def test_history_counts_autoparks_as_offender_signal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "runs"
    _write_run(
        d,
        "run-20260701T000000Z-1.jsonl",
        [
            {"kind": "autopark", "pick": {"title": "forked item"}, "reason": "fork"},
            {"kind": "autopark", "pick": {"title": "forked item"}, "reason": "fork again"},
        ],
    )
    rc = main(["history", "--stats-dir", str(d)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "forked item" in out and "2 auto-park(s)" in out
