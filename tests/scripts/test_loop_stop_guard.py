"""Regression oracle for scripts/loop_stop_guard.sh — the headless-loop Stop hook.

The hook is the STRUCTURAL prevention for the recurring `loop_next_todo` bg-yield
abandonment (memory project_loop_next_todo_no_land_root_causes.md): it blocks the
agent from ending its turn while (1) the working tree is dirty OR (2) the session
TRANSCRIPT shows a clean-tree background-job yield (a run_in_background op whose
callback headless never delivers), forcing it to finish synchronously or commit.
Expected outputs here are SPEC-derived (from the hook's documented contract), NOT
read off the implementation — so "all green" is a real oracle, not a tautology.

Contract under test (Claude Code Stop hook):
  stdin  = JSON {cwd, session_id, permission_mode, transcript_path}
  block  = stdout {"decision":"block","reason":...} + exit 0
  allow  = exit 0, empty stdout

Invariants:
  - block on a dirty tree (dirty takes precedence over the transcript signal)
  - block on a CLEAN tree when the transcript's last assistant turn backgrounded
    a Bash job (run_in_background) or narrates awaiting a completion callback
  - allow a CLEAN tree with no transcript, or whose last turn is a normal finish
    ("done, committed and pushed") — the callback markers are specific, so a
    completed pass is never falsely blocked
  - never block an interactive session (permission_mode == "default")
  - block at most ONCE per session (own re-entry guard; this version has no
    stop_hook_active field)
  - fail OPEN on any error (malformed stdin, non-repo cwd, ... -> allow)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "loop_stop_guard.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one committed file (clean tree)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.co")
    _git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def run_hook(stdin: object, *, tmpdir: Path) -> tuple[int, str]:
    """Invoke the hook with a JSON stdin. TMPDIR isolates the re-entry marker dir
    per test so one test's block-marker can't leak into another."""
    env = dict(os.environ, TMPDIR=str(tmpdir))
    payload = stdin if isinstance(stdin, str) else json.dumps(stdin)
    proc = subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout


def _is_block(stdout: str) -> bool:
    out = stdout.strip()
    if not out:
        return False
    obj = json.loads(out)
    return obj.get("decision") == "block"


def test_hook_is_executable() -> None:
    assert HOOK.exists(), f"hook missing: {HOOK}"
    assert os.access(HOOK, os.X_OK), "hook not executable"


def test_clean_tree_allows(repo: Path, tmp_path: Path) -> None:
    rc, out = run_hook(
        {"cwd": str(repo), "session_id": "s-clean", "permission_mode": "auto"},
        tmpdir=tmp_path,
    )
    assert rc == 0
    assert out.strip() == "", "clean tree must NOT block"


def test_dirty_tree_blocks(repo: Path, tmp_path: Path) -> None:
    (repo / "f.txt").write_text("base\nedit\n")  # dirty
    rc, out = run_hook(
        {"cwd": str(repo), "session_id": "s-dirty", "permission_mode": "auto"},
        tmpdir=tmp_path,
    )
    assert rc == 0
    assert _is_block(out), "dirty tree must block"
    reason = json.loads(out)["reason"]
    assert "NO-YIELD" in reason
    assert "f.txt" in reason, "reason must list the uncommitted file"
    assert "run_in_background" in reason, "reason must name the bg-yield trap"


def test_untracked_file_blocks(repo: Path, tmp_path: Path) -> None:
    (repo / "new_scratch.py").write_text("x = 1\n")  # untracked == dirty porcelain
    _rc, out = run_hook(
        {"cwd": str(repo), "session_id": "s-untracked", "permission_mode": "auto"},
        tmpdir=tmp_path,
    )
    assert _is_block(out)
    assert "new_scratch.py" in json.loads(out)["reason"]


def test_reentry_guard_blocks_once_per_session(repo: Path, tmp_path: Path) -> None:
    (repo / "f.txt").write_text("base\nedit\n")
    payload = {"cwd": str(repo), "session_id": "s-reentry", "permission_mode": "auto"}
    _rc1, out1 = run_hook(payload, tmpdir=tmp_path)
    _rc2, out2 = run_hook(payload, tmpdir=tmp_path)  # same session, still dirty
    assert _is_block(out1), "first stop must block"
    assert out2.strip() == "", "second stop in same session must allow (no infinite loop)"


def test_distinct_sessions_each_block(repo: Path, tmp_path: Path) -> None:
    (repo / "f.txt").write_text("base\nedit\n")
    _, out_a = run_hook(
        {"cwd": str(repo), "session_id": "sess-A", "permission_mode": "auto"},
        tmpdir=tmp_path,
    )
    _, out_b = run_hook(
        {"cwd": str(repo), "session_id": "sess-B", "permission_mode": "auto"},
        tmpdir=tmp_path,
    )
    assert _is_block(out_a) and _is_block(out_b), "the guard is per-session, not global"


def test_interactive_default_never_blocks(repo: Path, tmp_path: Path) -> None:
    (repo / "f.txt").write_text("base\nedit\n")  # dirty
    _rc, out = run_hook(
        {"cwd": str(repo), "session_id": "s-int", "permission_mode": "default"},
        tmpdir=tmp_path,
    )
    assert out.strip() == "", "interactive (default) sessions must never be blocked"


def test_missing_permission_mode_fails_open(repo: Path, tmp_path: Path) -> None:
    (repo / "f.txt").write_text("base\nedit\n")
    _rc, out = run_hook({"cwd": str(repo), "session_id": "s-noperm"}, tmpdir=tmp_path)
    assert out.strip() == "", "absent permission_mode must fail open (no block)"


def test_bypass_permissions_blocks(repo: Path, tmp_path: Path) -> None:
    (repo / "f.txt").write_text("base\nedit\n")
    _, out = run_hook(
        {"cwd": str(repo), "session_id": "s-bypass", "permission_mode": "bypassPermissions"},
        tmpdir=tmp_path,
    )
    assert _is_block(out), "the loop's bypassPermissions mode must still be guarded"


def test_malformed_stdin_fails_open(tmp_path: Path) -> None:
    rc, out = run_hook("not json at all", tmpdir=tmp_path)
    assert rc == 0
    assert out.strip() == "", "malformed stdin must fail open"


def test_non_repo_cwd_fails_open(tmp_path: Path) -> None:
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    rc, out = run_hook(
        {"cwd": str(notrepo), "session_id": "s-norepo", "permission_mode": "auto"},
        tmpdir=tmp_path,
    )
    assert rc == 0
    assert out.strip() == "", "a non-git cwd must fail open (git status empty)"


# --------------------------------------------------------------------------------------------------
# Clean-tree bg-yield detection (the transcript signal) — the layer that catches the `make test`
# stall the dirty-tree check (a clean tree) cannot see.
# --------------------------------------------------------------------------------------------------


def _assistant_line(*, text: str | None = None, background: bool = False) -> str:
    """One Claude-Code transcript JSONL line for an assistant turn — a text block and/or a
    run_in_background Bash tool_use, in the {type:assistant, message:{role,content:[...]}} shape."""
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if background:
        content.append(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "make test", "run_in_background": True},
            }
        )
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}})


def _transcript(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_clean_tree_bg_yield_narration_blocks(repo: Path, tmp_path: Path) -> None:
    """The item-17 case: clean tree, but the last assistant turn narrates awaiting a background-job
    completion callback. Must BLOCK, with the bg-yield (not dirty-tree) reason."""
    tx = _transcript(
        tmp_path,
        _assistant_line(text="Kicked off the run."),
        _assistant_line(
            text="~6% done, all green. I'll stop and let the completion notification re-invoke me."
        ),
    )
    _rc, out = run_hook(
        {
            "cwd": str(repo),
            "session_id": "s-yield",
            "permission_mode": "auto",
            "transcript_path": str(tx),
        },
        tmpdir=tmp_path,
    )
    assert _is_block(out), "a clean-tree bg-yield narration must block"
    reason = json.loads(out)["reason"]
    assert "BACKGROUND JOB" in reason
    assert "run_in_background" in reason
    assert "auto-park" in reason  # tells the agent the loop will park it if it can't finish


def test_clean_tree_run_in_background_tooluse_blocks(repo: Path, tmp_path: Path) -> None:
    """The last assistant turn backgrounded a Bash job and stopped — the intent signal, no narration
    needed. Must block on a clean tree."""
    tx = _transcript(tmp_path, _assistant_line(text="Starting the suite.", background=True))
    _rc, out = run_hook(
        {
            "cwd": str(repo),
            "session_id": "s-bgtool",
            "permission_mode": "auto",
            "transcript_path": str(tx),
        },
        tmpdir=tmp_path,
    )
    assert _is_block(out), "a clean-tree run_in_background yield must block"


def test_clean_tree_normal_finish_transcript_allows(repo: Path, tmp_path: Path) -> None:
    """A legitimately-finished pass (clean tree, a normal 'done, committed and pushed' final turn)
    must NOT be blocked — the callback markers are specific enough to avoid this false positive."""
    tx = _transcript(
        tmp_path,
        _assistant_line(text="Implemented the fix and added a test."),
        _assistant_line(
            text="Done. Item struck, committed and pushed to main. Working tree clean."
        ),
    )
    rc, out = run_hook(
        {
            "cwd": str(repo),
            "session_id": "s-done",
            "permission_mode": "auto",
            "transcript_path": str(tx),
        },
        tmpdir=tmp_path,
    )
    assert rc == 0
    assert out.strip() == "", "a normal completed pass must NOT be blocked"


def test_missing_transcript_file_allows_on_clean_tree(repo: Path, tmp_path: Path) -> None:
    """transcript_path pointing at a nonexistent file must fail open (clean tree -> allow), so the
    hardening can never wedge a pass when the transcript is unavailable."""
    rc, out = run_hook(
        {
            "cwd": str(repo),
            "session_id": "s-notx",
            "permission_mode": "auto",
            "transcript_path": str(tmp_path / "does-not-exist.jsonl"),
        },
        tmpdir=tmp_path,
    )
    assert rc == 0
    assert out.strip() == "", "an unreadable transcript must fail open on a clean tree"


def test_interactive_default_never_blocks_bg_yield(repo: Path, tmp_path: Path) -> None:
    """The transcript signal must still respect the interactive gate — a 'default' session is never
    blocked, even on a bg-yield transcript."""
    tx = _transcript(tmp_path, _assistant_line(text="I'll await its completion notification."))
    _rc, out = run_hook(
        {
            "cwd": str(repo),
            "session_id": "s-int-yield",
            "permission_mode": "default",
            "transcript_path": str(tx),
        },
        tmpdir=tmp_path,
    )
    assert out.strip() == "", "interactive sessions are never blocked, bg-yield or not"


def test_dirty_tree_takes_precedence_over_bg_yield(repo: Path, tmp_path: Path) -> None:
    """When the tree is dirty AND the transcript reads as a bg-yield, the higher-signal DIRTY reason
    (which lists the stranded files) wins."""
    (repo / "f.txt").write_text("base\nedit\n")  # dirty
    tx = _transcript(tmp_path, _assistant_line(text="I'll await the completion notification."))
    _rc, out = run_hook(
        {
            "cwd": str(repo),
            "session_id": "s-both",
            "permission_mode": "auto",
            "transcript_path": str(tx),
        },
        tmpdir=tmp_path,
    )
    assert _is_block(out)
    reason = json.loads(out)["reason"]
    assert "f.txt" in reason, "dirty reason (lists stranded files) must take precedence"
