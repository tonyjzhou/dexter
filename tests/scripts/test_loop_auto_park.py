"""Tests for the deterministic auto-park backstop (TODOS.md "Auto-park a `blocked` decision-fork
no-land and continue the drain").

Contract (SPEC-derived from the ticket's decision, NOT read off the implementation):

A `blocked` (decision-fork) no-land means the pass shaped a design but hit a go/no-go it cannot make
headless (no AskUserQuestion under `claude -p`). Before this backstop the loop `exit 3`'d and STRANDED
the whole ready backlog behind that one fork (the recurring trap — the last occurrence stranded 45
ready items and forced a manual re-run). The backstop, firing ONLY on `blocked` (never stalled /
no-land / ERROR / unknown), and ONLY when it is safe + productive:
  - PARKS the item: appends a `trigger-gated` marker to its **Priority:** line so next_todo re-buckets
    it PARKED (verified — the edit is re-parsed and written only if it actually re-buckets, never
    closing the item), and CONTINUES the drain to the next ready item;
  - COMMITS the one-line docs(todos) edit straight to main + pushes (no branch/PR/VERSION bump);
  - is capped to ONE auto-park per item per run — an item re-offered AFTER we parked it is a REAL
    stall the operator must see (park didn't stick / undecidable), never silent churn;
  - LOGS loudly (console + a `kind:autopark` run-JSONL event) so the deferred fork surfaces as the
    operator's decision QUEUE — it PARKS the fork, it never DECIDES it.

Four test levels (mirroring the established patterns in this directory):
  * `auto_park_decision` is pure — extracted by regex + run in isolation (like error_retry_decision).
  * `next_todo park` + `loop_stats park-summary/record-autopark` are the tested write/record pieces.
  * `auto_park_and_land` (the park+commit+push orchestration) is driven directly against a throwaway
    git repo with a bare origin — proving the real side effects, not just a return code.
  * END-TO-END: the loop is symlinked into a throwaway repo (so its `cd "$ROOT"` lands in the sandbox)
    with a bare origin + a fake `claude` that always escalates a decision fork; we assert it AUTO-PARKS
    both ready items and DRAINS (exit 0) — proving parks+commits+CONTINUES — while a non-blocked
    outcome still `exit 3`s unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "loop_next_todo.sh"
LOOP_STATS = ROOT / "scripts" / "loop_stats.py"


def _import(mod_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / "scripts" / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # register before exec so @dataclass can resolve the module
    spec.loader.exec_module(mod)
    return mod


next_todo = _import("next_todo", "next_todo.py")
loop_stats = _import("loop_stats", "loop_stats.py")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _extract_fn(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.DOTALL | re.MULTILINE)
    assert m, f"{name}() not found in loop_next_todo.sh"
    return m.group(0)


# --------------------------------------------------------------------------------------------------
# bash syntax + pure auto_park_decision gate
# --------------------------------------------------------------------------------------------------


def test_script_passes_bash_syntax_check() -> None:
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _decide(already_parked: str, branch: str, dirty: str) -> str:
    harness = _extract_fn("auto_park_decision") + (
        f'\nauto_park_decision "{already_parked}" "{branch}" "{dirty}"\n'
    )
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("already_parked", "branch", "dirty", "expected"),
    [
        ("0", "main", "0", "park"),  # the only "park" case: fresh, on main, clean
        ("1", "main", "0", "stop"),  # already parked this run -> real stall, don't churn
        ("0", "claude/x", "0", "stop"),  # off main -> can't commit docs to main
        ("0", "main", "1", "stop"),  # dirty tree -> don't fold unrelated edits into the commit
        ("1", "claude/x", "1", "stop"),
    ],
)
def test_auto_park_decision_gate(already_parked, branch, dirty, expected) -> None:
    assert _decide(already_parked, branch, dirty) == expected


def test_stalled_blocked_and_default_arms_auto_park_unknown_still_exit3() -> None:
    """The THREE un-drainable-headless no-lands self-heal by auto-parking: `blocked` (a decision
    fork), `stalled` (a background-job yield), and the generic no-progress (`*)`) default — the last
    of which was the FINAL hard-stop that could still terminate a whole drain (a promote-then-present
    pass read `terminal_signal:null` → generic `no-land` → the default arm exit-3'd, stranding 12
    ready items). Each attempts the shared `attempt_auto_park` helper and keeps a manual-stop
    fallback (exit 3). Only the `unknown` arm (an indeterminate land state — a degraded repo the
    operator must resolve) still hard-stops without parking; `ERROR` has its own retry/stop logic."""
    text = SCRIPT.read_text(encoding="utf-8")
    # Isolate the `case "$prev_outcome"` block (from the case to its esac).
    block = re.search(r'case "\$prev_outcome" in.*?\n      esac', text, re.DOTALL)
    assert block, "prev_outcome case block not found"
    body = block.group(0)
    # The stalled + blocked + default arms attempt the shared auto-park (the pure decision helper is
    # called INSIDE attempt_auto_park, not the arms). Decoupling the loop's self-healing from the
    # brittle stall/fork classifier means ANY clean-tree same-pick no-land parks and continues.
    # Count CALL SITES (`if attempt_auto_park "..."`), not the bare token — a comment may reference
    # the helper by name without calling it.
    assert body.count("if attempt_auto_park ") == 3, (
        "the stalled, blocked, AND default arms must auto-park"
    )
    # NOTE: the default arm is anchored as "\n        *)" (newline + EXACTLY 8 spaces) — a bare
    # "        *)" is a substring of the ERROR arm's nested 12-space "            *)", so it would
    # split on the wrong arm.
    for arm in ("stalled)", "blocked)", "\n        *)"):
        seg = body.split(arm, 1)[1].split(";;", 1)[0]
        assert "attempt_auto_park" in seg, f"the {arm!r} arm must attempt auto-park"
        assert "exit 3" in seg, f"the {arm!r} arm must keep a manual-stop fallback (exit 3)"
    for arm in ("unknown)",):
        seg = body.split(arm, 1)[1].split(";;", 1)[0]
        assert "attempt_auto_park" not in seg, f"the {arm!r} arm must NOT auto-park"
        assert "exit 3" in seg, f"the {arm!r} arm must still exit 3"


# --------------------------------------------------------------------------------------------------
# next_todo park — verify-then-write, idempotent, closed-marker-safe
# --------------------------------------------------------------------------------------------------

_TODOS_FIXTURE = """# TODOS

## Section

### A ready feature item
**Priority:** P1
**What:** do a thing

### An already-parked item
**Priority:** P2 (trigger-gated: awaiting X)
**What:** parked

### A heading with no priority line
Just prose.
"""


def _write(tmp_path: Path, text: str = _TODOS_FIXTURE) -> Path:
    p = tmp_path / "TODOS.md"
    p.write_text(text, encoding="utf-8")
    return p


def _bucket_at(path: Path, line: int) -> str | None:
    items, _ = next_todo.parse(path.read_text())
    return next((it.bucket for it in items if it.line == line), None)


def test_park_reburckets_ready_item_parked(tmp_path: Path) -> None:
    p = _write(tmp_path)
    assert next_todo.park_item(p, 5, "agent escalated a Track-A fork") == 0
    assert _bucket_at(p, 5) == "parked"
    assert "trigger-gated" in p.read_text().splitlines()[5]  # the P1 priority line (0-based idx 5)


def test_park_is_idempotent_on_already_parked(tmp_path: Path) -> None:
    p = _write(tmp_path)
    before = p.read_text()
    assert next_todo.park_item(p, 9, "x") == 0  # already parked
    assert p.read_text() == before  # no double-append


def test_park_refuses_item_with_no_priority_line(tmp_path: Path) -> None:
    p = _write(tmp_path)
    before = p.read_text()
    assert next_todo.park_item(p, 13, "x") != 0
    assert p.read_text() == before  # nothing written


def test_park_out_of_range_line(tmp_path: Path) -> None:
    p = _write(tmp_path)
    assert next_todo.park_item(p, 999, "x") == 3


def test_park_missing_file(tmp_path: Path) -> None:
    assert next_todo.park_item(tmp_path / "nope.md", 1, "x") == 2


def test_park_sanitizes_closed_markers_and_still_parks(tmp_path: Path) -> None:
    """A reason containing closed-markers must NOT close the item (which would drop it from the
    backlog entirely) — it is sanitized and the item still buckets PARKED."""
    p = _write(tmp_path, "### Feature\n**Priority:** P1\n**What:** thing\n")
    assert next_todo.park_item(p, 1, "this is RESOLVED → done mostly done ~~x~~ (paren)") == 0
    items, closed = next_todo.parse(p.read_text())
    assert closed == 0  # not falsely closed
    assert next((it.bucket for it in items if it.line == 1), None) == "parked"


def test_park_preserves_every_other_byte(tmp_path: Path) -> None:
    p = _write(tmp_path)
    before = p.read_text().splitlines(keepends=True)
    next_todo.park_item(p, 5, "fork")
    after = p.read_text().splitlines(keepends=True)
    assert len(before) == len(after)
    for i, (b, a) in enumerate(zip(before, after, strict=True)):
        if i == 5:
            continue  # the one edited priority line
        assert b == a, f"line {i} changed unexpectedly"


# --------------------------------------------------------------------------------------------------
# loop_stats — park-summary, record-autopark, and pass-tally isolation
# --------------------------------------------------------------------------------------------------


def _blocked_pass_record(excerpt: str) -> dict:
    return {
        "iter": 1,
        "pick": {"priority": "P1", "title": "Forky item", "line": 3},
        "rc": 0,
        "landed": False,
        "head_advanced": False,
        "dirty_count": 0,
        "new_prs": [],
        "claude": {"parsed": True, "terminal_signal": "decision_fork", "terminal_excerpt": excerpt},
    }


def test_park_summary_extracts_one_line_capped(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    long = "I need your call before the build. " * 20  # > cap
    rf.write_text(json.dumps(_blocked_pass_record(long)) + "\n")
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "park-summary", "--run-file", str(rf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out and "\n" not in out
    assert len(out) <= loop_stats._PARK_SUMMARY_MAX


def test_park_summary_empty_when_no_excerpt(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps({"iter": 1, "landed": True, "claude": {"parsed": True}}) + "\n")
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "park-summary", "--run-file", str(rf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == ""


def test_record_autopark_is_isolated_from_pass_tallies(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("genuine fork — your call?")) + "\n")
    subprocess.run(
        [
            "python3",
            str(LOOP_STATS),
            "record-autopark",
            "--run-file",
            str(rf),
            "--iter",
            "1",
            "--priority",
            "P1",
            "--title",
            "Forky item",
            "--line",
            "3",
            "--reason",
            "a design fork",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # The autopark event is EXCLUDED from pass records...
    passes = loop_stats._load_records(str(rf))
    assert len(passes) == 1 and passes[0].get("kind") != "autopark"
    # ...and last-outcome still reads the blocked PASS, not the autopark event.
    lo = subprocess.run(
        ["python3", str(LOOP_STATS), "last-outcome", "--run-file", str(rf)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert lo == "blocked"
    # ...while _load_autoparks surfaces it as the decision queue.
    aps = loop_stats._load_autoparks(str(rf))
    assert len(aps) == 1 and aps[0]["pick"]["title"] == "Forky item"


def test_summary_renders_decision_queue(tmp_path: Path) -> None:
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("fork")) + "\n")
    subprocess.run(
        [
            "python3",
            str(LOOP_STATS),
            "record-autopark",
            "--run-file",
            str(rf),
            "--iter",
            "1",
            "--priority",
            "P1",
            "--title",
            "Forky item",
            "--line",
            "3",
            "--reason",
            "a design fork the operator must decide",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "summary", "--run-file", str(rf), "--reason", "test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "auto-parked 1 decision-fork item(s)" in out
    assert "Forky item" in out
    assert "a design fork the operator must decide" in out


# --------------------------------------------------------------------------------------------------
# auto_park_and_land — real side effects against a throwaway repo + bare origin
# --------------------------------------------------------------------------------------------------

_WORK_TODOS = "# TODOS\n\n## S\n\n### A forky item\n**Priority:** P1\n**What:** decide me\n"


def _build_work_repo(tmp_path: Path, todos_text: str = _WORK_TODOS) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work.mkdir()
    (work / "TODOS.md").write_text(todos_text, encoding="utf-8")
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@e.st")
    _git(work, "config", "user.name", "test")
    _git(work, "add", "TODOS.md")
    _git(work, "commit", "-q", "-m", "seed")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work, origin


def _run_auto_park_and_land(
    work: Path, run_file: Path, line: int
) -> subprocess.CompletedProcess[str]:
    harness = _extract_fn("auto_park_and_land") + (
        f'\nauto_park_and_land "{work}" "{work}/TODOS.md" "{run_file}" {line} "A forky item" P1 7\n'
        'echo "RC=$?"\n'
    )
    # cwd = the REAL repo so `python3 scripts/*.py` resolves; git -C operates on the temp work repo.
    return subprocess.run(
        ["bash", "-c", harness], cwd=ROOT, capture_output=True, text=True, timeout=60
    )


def test_auto_park_and_land_parks_commits_and_pushes(tmp_path: Path) -> None:
    work, origin = _build_work_repo(tmp_path)
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("a genuine fork — which option?")) + "\n")

    r = _run_auto_park_and_land(work, rf, 5)  # heading "### A forky item" is line 5
    assert "RC=0" in r.stdout, r.stdout + r.stderr

    # 1) the item is parked in the work tree
    assert "trigger-gated" in (work / "TODOS.md").read_text()
    assert _bucket_at(work / "TODOS.md", 5) == "parked"
    # 2) a docs(todos) commit exists on the work tree...
    log = subprocess.run(
        ["git", "-C", str(work), "log", "-1", "--pretty=%s"], capture_output=True, text=True
    ).stdout
    assert "docs(todos): auto-park decision-fork item" in log
    # 3) ...and it PUSHED — origin's main ref advanced to the work HEAD
    work_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    origin_main = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "main"], capture_output=True, text=True
    ).stdout.strip()
    assert work_head == origin_main
    # 4) a loud decision-queue event was recorded
    assert loop_stats._load_autoparks(str(rf))


def test_auto_park_and_land_fails_clean_on_bad_line(tmp_path: Path) -> None:
    """A park that can't verify (out-of-range line) returns non-zero and leaves NOTHING behind —
    no commit, no dirty tree — so the caller falls back to the manual stop."""
    work, _ = _build_work_repo(tmp_path)
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("fork")) + "\n")
    head_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    r = _run_auto_park_and_land(work, rf, 999)  # no item at line 999
    assert "RC=0" not in r.stdout, r.stdout + r.stderr  # a non-zero return code
    # tree clean, no new commit
    assert (
        subprocess.run(
            ["git", "-C", str(work), "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        == ""
    )
    head_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head_after == head_before


# --------------------------------------------------------------------------------------------------
# END-TO-END: the loop auto-parks both forks and DRAINS (parks + commits + CONTINUES)
# --------------------------------------------------------------------------------------------------

_FAKE_CLAUDE_FORK = r"""#!/usr/bin/env bash
is_pass=0
for a in "$@"; do [ "$a" = "/next-todo" ] && is_pass=1; done
if [ "$is_pass" = "1" ]; then
  printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":5,"total_cost_usd":1.0,"duration_ms":1000,"duration_api_ms":900,"result":"I shaped the design but hit a genuine fork I cannot resolve headless. Which option do you want before I build the multi-hour thing?","usage":{"input_tokens":10,"output_tokens":5}}'
  exit 0
fi
printf '{"type":"system","subtype":"init","model":"resolved-opus-test"}\n'
exit 0
"""

_FAKE_GH = "#!/usr/bin/env bash\nexit 1\n"

# Fake agent that ends every /next-todo pass with a background-job YIELD (a clean-tree `stalled`
# no-land): "I launched make test in the background and will await its completion notification" — the
# exact narration loop_stats classifies `bg_yield` → `stalled`. Mirrors _FAKE_CLAUDE_FORK's shape.
_FAKE_CLAUDE_BG_YIELD = r"""#!/usr/bin/env bash
is_pass=0
for a in "$@"; do [ "$a" = "/next-todo" ] && is_pass=1; done
if [ "$is_pass" = "1" ]; then
  printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":5,"total_cost_usd":1.0,"duration_ms":1000,"duration_api_ms":900,"result":"I launched make test in the background and will await its completion notification before finishing — polling would just burn budget and cache.","usage":{"input_tokens":10,"output_tokens":5}}'
  exit 0
fi
printf '{"type":"system","subtype":"init","model":"resolved-opus-test"}\n'
exit 0
"""

# Fake agent that ends every /next-todo pass with a GENERIC no-land — the promote-then-present shape
# (the pass states a pick + reasoning and ends its turn without building), which carries NO bg-yield
# and NO decision-fork marker, so loop_stats records `terminal_signal:null` → `_pass_outcome` returns
# a generic `no-land`. This is the exact class that dodged the classifier and HARD-STOPPED the drain
# (project_loop_next_todo_no_land_root_causes, 17th occurrence); the default arm must now auto-park it.
_FAKE_CLAUDE_NO_LAND = r"""#!/usr/bin/env bash
is_pass=0
for a in "$@"; do [ "$a" = "/next-todo" ] && is_pass=1; done
if [ "$is_pass" = "1" ]; then
  printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"num_turns":5,"total_cost_usd":1.0,"duration_ms":1000,"duration_api_ms":900,"result":"I have everything I need to make the call. Let me state the pick and the reasoning. Pick: the highest-value ready item. I mapped the subsystem and the fill triplet and selected it.","usage":{"input_tokens":10,"output_tokens":5}}'
  exit 0
fi
printf '{"type":"system","subtype":"init","model":"resolved-opus-test"}\n'
exit 0
"""

_SANDBOX_TODOS_2 = """# TODOS

### Item A needs a design decision
**Priority:** P1
**Why:** the fake agent escalates a fork here (parked first).

### Item B also needs a design decision
**Priority:** P2
**Why:** the loop must CONTINUE here after A is parked.
"""

_SCRIPT_LINKS = [
    "loop_next_todo.sh",
    "loop_stats.py",
    "next_todo.py",
    "loop_next_todo.settings.json",
    "loop_stop_guard.sh",
]


def _make_fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    exe = fakebin / name
    exe.write_text(body)
    exe.chmod(0o755)
    return fakebin


def _build_sandbox(tmp_path: Path, todos_text: str = _SANDBOX_TODOS_2) -> tuple[Path, Path]:
    sandbox = tmp_path / "sandbox"
    (sandbox / "scripts").mkdir(parents=True)
    for name in _SCRIPT_LINKS:
        (sandbox / "scripts" / name).symlink_to(ROOT / "scripts" / name)
    (sandbox / "TODOS.md").write_text(todos_text, encoding="utf-8")
    _git(sandbox, "init", "-q")
    _git(sandbox, "config", "user.email", "t@e.st")
    _git(sandbox, "config", "user.name", "test")
    _git(sandbox, "add", "-A")  # TODOS.md + the script symlinks, so the tree is clean post-pass
    _git(sandbox, "commit", "-q", "-m", "seed")
    _git(sandbox, "branch", "-M", "main")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    _git(sandbox, "remote", "add", "origin", str(origin))
    _git(sandbox, "push", "-q", "-u", "origin", "main")
    return sandbox, origin


def _run_loop(
    sandbox: Path, tmp_path: Path, claude_body: str = _FAKE_CLAUDE_FORK
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fakebin = _make_fake_bin(tmp_path, "claude", claude_body)
    _make_fake_bin(tmp_path, "gh", _FAKE_GH)
    stats_dir = tmp_path / "stats"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_")}
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
    env["LOOP_HEARTBEAT_SECS"] = "0"
    env["LOOP_NO_CAFFEINATE"] = "1"
    env["LOOP_MAX_ITERS"] = "12"  # safety net: a park-then-loop bug hits this (exit 0), never spins
    env["LOOP_STATS_DIR"] = str(stats_dir)
    proc = subprocess.run(
        ["bash", str(sandbox / "scripts" / "loop_next_todo.sh")],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, stats_dir


def test_end_to_end_auto_parks_both_forks_and_drains(tmp_path: Path) -> None:
    sandbox, origin = _build_sandbox(tmp_path)
    proc, stats_dir = _run_loop(sandbox, tmp_path)
    combined = proc.stdout + proc.stderr

    # Drained cleanly: both forks were parked, then no ready item remained.
    assert proc.returncode == 0, f"expected drain exit 0, got {proc.returncode}\n{combined}"
    assert combined.count("AUTO-PARKED (decision fork)") == 2, combined
    # Both items are now trigger-gated in the sandbox TODOS.md.
    todos = (sandbox / "TODOS.md").read_text()
    items, _ = next_todo.parse(todos)
    parked = {it.title for it in items if it.bucket == "parked"}
    assert "Item A needs a design decision" in parked
    assert "Item B also needs a design decision" in parked
    # The decision queue is durable: two kind:autopark events in the run JSONL.
    run_files = list(stats_dir.glob("run-*.jsonl"))
    assert len(run_files) == 1
    aps = loop_stats._load_autoparks(str(run_files[0]))
    assert len(aps) == 2
    # And the end-of-run summary surfaces it.
    assert "auto-parked 2 decision-fork item(s)" in combined
    # origin/main advanced with the two docs(todos) park commits.
    origin_log = subprocess.run(
        ["git", "-C", str(origin), "log", "--pretty=%s"], capture_output=True, text=True
    ).stdout
    assert origin_log.count("docs(todos): auto-park decision-fork item") == 2


def test_end_to_end_auto_parks_both_stalls_and_drains(tmp_path: Path) -> None:
    """The bg-yield SIBLING of the decision-fork e2e, and the regression for the terminal-stall bug: a
    `stalled` no-land (the agent yielded to a background job whose completion callback headless never
    delivers) now AUTO-PARKS + CONTINUES too, instead of `exit 3`-ing the whole drain (which once left
    43 ready items stranded on one `make test` bg-yield). Both stalled items park and the loop drains,
    labeled as STALLED (not decision forks) in the queue."""
    sandbox, origin = _build_sandbox(tmp_path)
    proc, stats_dir = _run_loop(sandbox, tmp_path, claude_body=_FAKE_CLAUDE_BG_YIELD)
    combined = proc.stdout + proc.stderr

    # Drained cleanly (exit 0) rather than terminating on the first stall.
    assert proc.returncode == 0, f"expected drain exit 0, got {proc.returncode}\n{combined}"
    assert combined.count("AUTO-PARKED (bg-yield stall)") == 2, combined
    # Both items trigger-gated in the sandbox TODOS.md.
    items, _ = next_todo.parse((sandbox / "TODOS.md").read_text())
    assert sum(1 for it in items if it.bucket == "parked") == 2
    # The decision queue labels them as STALLED, and each event carries the bg-yield kind.
    assert "auto-parked 2 stalled item(s)" in combined, combined
    run_files = list(stats_dir.glob("run-*.jsonl"))
    assert len(run_files) == 1
    aps = loop_stats._load_autoparks(str(run_files[0]))
    assert len(aps) == 2
    assert all(ap.get("park_kind") == "bg-yield" for ap in aps), aps
    # The park commits use the stalled-item message, not the decision-fork one.
    origin_log = subprocess.run(
        ["git", "-C", str(origin), "log", "--pretty=%s"], capture_output=True, text=True
    ).stdout
    assert origin_log.count("docs(todos): auto-park stalled item") == 2
    assert "auto-park decision-fork item" not in origin_log


def test_end_to_end_auto_parks_generic_no_lands_and_drains(tmp_path: Path) -> None:
    """The GENERIC-no-land sibling of the two e2e tests above, and the regression for the FINAL
    hard-stop: a same-pick no-land the classifier could NOT tag stall/fork (a promote-then-present
    pass — `terminal_signal:null`) now AUTO-PARKS + CONTINUES too, instead of `exit 3`-ing the whole
    drain (17th occurrence: one such pass stranded 12 ready items). Both items park and the loop
    drains, labeled as NO-LAND (not decision forks / stalls) in the queue."""
    sandbox, origin = _build_sandbox(tmp_path)
    proc, stats_dir = _run_loop(sandbox, tmp_path, claude_body=_FAKE_CLAUDE_NO_LAND)
    combined = proc.stdout + proc.stderr

    # Drained cleanly (exit 0) rather than terminating on the first generic no-land.
    assert proc.returncode == 0, f"expected drain exit 0, got {proc.returncode}\n{combined}"
    assert combined.count("AUTO-PARKED (no-land)") == 2, combined
    # Both items trigger-gated in the sandbox TODOS.md.
    items, _ = next_todo.parse((sandbox / "TODOS.md").read_text())
    assert sum(1 for it in items if it.bucket == "parked") == 2
    # The decision queue labels them as NO-LAND, and each event carries the no-land kind.
    assert "auto-parked 2 no-land item(s)" in combined, combined
    run_files = list(stats_dir.glob("run-*.jsonl"))
    assert len(run_files) == 1
    aps = loop_stats._load_autoparks(str(run_files[0]))
    assert len(aps) == 2
    assert all(ap.get("park_kind") == "no-land" for ap in aps), aps
    # The park commits use the no-land message, not the decision-fork / stalled one.
    origin_log = subprocess.run(
        ["git", "-C", str(origin), "log", "--pretty=%s"], capture_output=True, text=True
    ).stdout
    assert origin_log.count("docs(todos): auto-park no-land item") == 2
    assert "auto-park decision-fork item" not in origin_log
    assert "auto-park stalled item" not in origin_log


def test_summary_labels_all_three_kinds(tmp_path: Path) -> None:
    """The 3-way decision queue: a run that parked one of EACH kind (fork + stall + no-land) must
    enumerate all three in the mixed label, in a stable order — proving the no-land kind renders
    distinctly and doesn't fold into the decision-fork bucket."""
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("fork")) + "\n")

    def _record(kind: str, title: str, line: int) -> None:
        subprocess.run(
            [
                "python3",
                str(LOOP_STATS),
                "record-autopark",
                "--run-file",
                str(rf),
                "--iter",
                "1",
                "--priority",
                "P2",
                "--title",
                title,
                "--line",
                str(line),
                "--reason",
                "r",
                "--kind",
                kind,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    _record("decision fork", "A fork", 3)
    _record("bg-yield", "A stall", 9)
    _record("no-land", "A no-land", 12)
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "summary", "--run-file", str(rf), "--reason", "test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "auto-parked 3 parked item(s) (1 decision-fork, 1 stalled, 1 no-land)" in out, out


def test_summary_labels_all_no_land(tmp_path: Path) -> None:
    """An all-no-land run gets the terse single-kind label, mirroring the all-stalled / all-fork cases."""
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("x")) + "\n")
    for i, ln in enumerate((3, 9)):
        subprocess.run(
            [
                "python3",
                str(LOOP_STATS),
                "record-autopark",
                "--run-file",
                str(rf),
                "--iter",
                "1",
                "--priority",
                "P4",
                "--title",
                f"item {i}",
                "--line",
                str(ln),
                "--reason",
                "r",
                "--kind",
                "no-land",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "summary", "--run-file", str(rf), "--reason", "test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "auto-parked 2 no-land item(s)" in out, out


def test_summary_labels_mixed_and_stalled_autoparks_by_kind(tmp_path: Path) -> None:
    """The decision queue must label a stalled park honestly. A run that parked one decision fork AND
    one stall renders both counts; an all-stalled run says "stalled item(s)"."""
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("fork")) + "\n")

    def _record(kind: str, title: str, line: int) -> None:
        subprocess.run(
            [
                "python3",
                str(LOOP_STATS),
                "record-autopark",
                "--run-file",
                str(rf),
                "--iter",
                "1",
                "--priority",
                "P2",
                "--title",
                title,
                "--line",
                str(line),
                "--reason",
                "r",
                "--kind",
                kind,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    _record("decision fork", "A fork", 3)
    _record("bg-yield", "A stall", 9)
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "summary", "--run-file", str(rf), "--reason", "test"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "auto-parked 2 parked item(s) (1 decision-fork, 1 stalled)" in out, out


# --------------------------------------------------------------------------------------------------
# Adversarial-review regressions (each pins a bug the review found + I fixed)
# --------------------------------------------------------------------------------------------------


def test_auto_park_and_land_cleans_tree_on_commit_failure(tmp_path: Path) -> None:
    """Review finding (HIGH): a COMMIT failure (a failing commit hook, disk-full) must leave the tree
    fully CLEAN. The buggy revert `git checkout -- TODOS.md` restored from the INDEX — which already
    held the staged park edit — leaving `M TODOS.md` dirty, which then freezes sync_main forever.
    The fix restores from HEAD."""
    work, _ = _build_work_repo(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "commit-msg").write_text("#!/usr/bin/env bash\nexit 1\n")  # always reject the commit
    (hooks / "commit-msg").chmod(0o755)
    _git(work, "config", "core.hooksPath", str(hooks))
    rf = tmp_path / "run.jsonl"
    rf.write_text(json.dumps(_blocked_pass_record("a fork")) + "\n")
    head_before = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    r = _run_auto_park_and_land(work, rf, 5)
    assert "RC=0" not in r.stdout, r.stdout + r.stderr  # commit failed → non-zero return
    # THE FIX: tree fully clean (nothing staged, nothing dirty), no bogus marker, no stray commit.
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip()
    assert status == "", f"tree not clean after commit-failure: {status!r}"
    assert "trigger-gated" not in (work / "TODOS.md").read_text()
    head_after = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head_after == head_before


_SANDBOX_TODOS_SAME_TITLE = """# TODOS

### Duplicate heading
**Priority:** P1
**Why:** first item with this heading — parked first.

### Duplicate heading
**Priority:** P2
**Why:** a DISTINCT item that reuses the heading — must ALSO get parked.
"""


def test_end_to_end_cap_keys_on_pick_not_title(tmp_path: Path) -> None:
    """Review finding (MEDIUM): the one-park cap must key on the full $PICK (title+line), not the bare
    title — else two distinct items that reuse a heading collide and the drain false-stops on the
    second. With the fix, both same-titled forks get parked and the loop drains."""
    sandbox, _ = _build_sandbox(tmp_path, _SANDBOX_TODOS_SAME_TITLE)
    proc, stats_dir = _run_loop(sandbox, tmp_path)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"expected drain exit 0, got {proc.returncode}\n{combined}"
    # BOTH same-titled items parked (the $TITLE-keyed cap would have stopped on the second).
    assert combined.count("AUTO-PARKED (decision fork)") == 2, combined
    items, _ = next_todo.parse((sandbox / "TODOS.md").read_text())
    assert sum(1 for it in items if it.bucket == "parked") == 2
    run_files = list(stats_dir.glob("run-*.jsonl"))
    assert len(loop_stats._load_autoparks(str(run_files[0]))) == 2
