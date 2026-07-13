"""Regression oracle for the transient-ERROR auto-retry in scripts/loop_next_todo.sh.

Contract (SPEC-derived from the decision that motivated it, NOT read off the implementation):

A pass that ends with a HARD error — rc≠0 / is_error=true, e.g. an
"API Error: Connection closed mid-response" stream drop — means the picked item was NEVER
adjudicated: the agent died mid-orientation, not on a genuine no-progress decision. Pre-retry,
the spin-guard treated that identically to a real spin and halted the ENTIRE drain on the first
blip (observed 3/3 across the run history — every "Connection closed" disconnect terminated its
run with zero retries, once as early as pass 2, stranding ~31 ready items). The fix:

  - On a same-pick spin whose previous pass was `ERROR`, retry the SAME pick up to
    LOOP_ERROR_RETRIES times (default 2; a fresh `claude -p` = a fresh connection) before
    spin-stopping. So a persistently-erroring item is attempted exactly 1 + LOOP_ERROR_RETRIES
    times, then the loop exits 3 with a SUSTAINED-outage message (distinct from the generic
    "inspect, then re-run").
  - LOOP_ERROR_RETRIES=0 restores the pre-retry behaviour: a transient error spin-stops at once
    (item attempted exactly once).
  - The seam this rides on: `loop_stats.py last-outcome` must emit the literal string "ERROR" for
    an rc≠0 / is_error pass — that is exactly the token the new `case` arm matches (bash `case` is
    case-sensitive, so a silent rename in loop_stats would break the arm without a syntax error).
  - LOOP_ERROR_RETRIES / LOOP_ERROR_RETRY_BACKOFF_SECS validate as non-negative integers and the
    policy is surfaced on the run-config banner.

Two test levels, mirroring the established patterns in this directory:
  * `error_retry_decision` is a pure, side-effect-free shell function — extracted by regex and run
    in isolation (exactly like test_loop_new_prs_json.py does for `new_prs_json`).
  * The WIRING (fall-through re-run + counter + stop) is proven END-TO-END: the loop script is
    symlinked into a throwaway git repo so its `cd "$ROOT"` lands in the sandbox (never the real
    repo), a fake `claude` always returns rc=1, and we assert the pick is attempted exactly
    1 + LOOP_ERROR_RETRIES times then the loop exits 3 — a spy on the arithmetic would only prove
    the function returns "retry"/"stop", not that the loop actually re-runs then gives up.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "loop_next_todo.sh"
LOOP_STATS = ROOT / "scripts" / "loop_stats.py"


# --------------------------------------------------------------------------------------------------
# bash syntax + pure `error_retry_decision` contract
# --------------------------------------------------------------------------------------------------


def test_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _extract_fn(name: str) -> str:
    """Pull just the `<name>()` definition out of the loop script (it has no standalone
    entrypoint; sourcing the whole script would run the loop). Mirrors test_loop_new_prs_json.py."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.DOTALL | re.MULTILINE)
    assert m, f"{name}() not found in loop_next_todo.sh"
    return m.group(0)


def _decide(spent: int, limit: int) -> str:
    harness = (
        "set -euo pipefail\n"
        + _extract_fn("error_retry_decision")
        + f"\nerror_retry_decision {spent} {limit}\n"
    )
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("spent", "limit", "expected"),
    [
        (0, 2, "retry 1"),  # first error → first retry, attempt #1
        (1, 2, "retry 2"),  # second error → second retry, attempt #2
        (2, 2, "stop"),  # budget exhausted (2 retries taken) → stop
        (0, 0, "stop"),  # retries disabled → immediate stop (pre-retry behaviour)
        (0, 1, "retry 1"),  # single-retry budget: one retry then...
        (1, 1, "stop"),  # ...stop
        (5, 2, "stop"),  # defensive: over-budget never wraps back to retry
    ],
)
def test_error_retry_decision_contract(spent: int, limit: int, expected: str) -> None:
    assert _decide(spent, limit) == expected


# --------------------------------------------------------------------------------------------------
# cross-file SEAM: loop_stats last-outcome must say "ERROR" for the arm to match
# --------------------------------------------------------------------------------------------------


def _last_outcome(records: list[dict], tmp_path: Path) -> str:
    run_file = tmp_path / "run.jsonl"
    run_file.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    out = subprocess.run(
        ["python3", str(LOOP_STATS), "last-outcome", "--run-file", str(run_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_last_outcome_is_ERROR_for_rc_nonzero(tmp_path: Path) -> None:
    """rc≠0 alone classifies ERROR — the token the new case arm keys on (case-sensitive)."""
    assert _last_outcome([{"rc": 1, "claude": {"is_error": False}}], tmp_path) == "ERROR"


def test_last_outcome_is_ERROR_for_is_error_true(tmp_path: Path) -> None:
    """is_error=true (even with rc==0) also classifies ERROR — both API-drop signatures covered."""
    assert _last_outcome([{"rc": 0, "claude": {"is_error": True}}], tmp_path) == "ERROR"


def test_last_outcome_not_ERROR_for_clean_pass(tmp_path: Path) -> None:
    """A landed pass must NOT read ERROR (else the retry-budget reset would never fire)."""
    rec = {"rc": 0, "landed": True, "head_advanced": True, "claude": {"is_error": False}}
    assert _last_outcome([rec], tmp_path) == "landed"


# --------------------------------------------------------------------------------------------------
# config validation + run-config banner (driven via LOOP_DRY_RUN=1, mirroring test_loop_model_effort)
# --------------------------------------------------------------------------------------------------

_FAKE_CLAUDE_INIT = (
    "#!/usr/bin/env bash\n"
    'printf \'{"type":"system","subtype":"init","model":"resolved-opus-test"}\\n\'\n'
)


def _make_fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    exe = fakebin / name
    exe.write_text(body)
    exe.chmod(0o755)
    return fakebin


def _dry_run(env_overrides: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    fakebin = _make_fake_bin(tmp_path, "claude", _FAKE_CLAUDE_INIT)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_")}
    env["LOOP_DRY_RUN"] = "1"
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )


def _banner_field(stdout: str, label: str) -> str:
    m = re.search(rf"^  {re.escape(label)}\s*: (.+)$", stdout, re.MULTILINE)
    assert m, f"banner line for {label!r} not found in:\n{stdout}"
    return m.group(1).strip()


def test_default_error_retry_banner(tmp_path: Path) -> None:
    result = _dry_run({}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    line = _banner_field(result.stdout, "error-retry")
    assert "up to 2" in line and "same-item" in line and "10s backoff" in line


def test_error_retry_disabled_banner(tmp_path: Path) -> None:
    result = _dry_run({"LOOP_ERROR_RETRIES": "0"}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "off" in _banner_field(result.stdout, "error-retry")


def test_error_retry_custom_values_banner(tmp_path: Path) -> None:
    result = _dry_run({"LOOP_ERROR_RETRIES": "5", "LOOP_ERROR_RETRY_BACKOFF_SECS": "3"}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    line = _banner_field(result.stdout, "error-retry")
    assert "up to 5" in line and "same-item" in line and "3s backoff" in line


@pytest.mark.parametrize("var", ["LOOP_ERROR_RETRIES", "LOOP_ERROR_RETRY_BACKOFF_SECS"])
def test_non_integer_is_a_hard_error(var: str, tmp_path: Path) -> None:
    result = _dry_run({var: "abc"}, tmp_path)
    assert result.returncode != 0
    assert var in result.stdout
    assert "ERROR" in result.stdout


# --------------------------------------------------------------------------------------------------
# END-TO-END wiring: a persistently-erroring pick is attempted exactly 1 + LOOP_ERROR_RETRIES times
# --------------------------------------------------------------------------------------------------

# Fake `claude`: for a real pass (`/next-todo` in argv) bump a counter file and emit an rc=1
# is_error result; for the resolve_model_version probe (`hi`, no `/next-todo`) emit an init line and
# exit 0 so it is neither counted nor treated as a pass.
_FAKE_CLAUDE_ERR = r"""#!/usr/bin/env bash
is_pass=0
for a in "$@"; do [ "$a" = "/next-todo" ] && is_pass=1; done
if [ "$is_pass" = "1" ]; then
  n=$(cat "$LOOP_TEST_COUNTER" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$LOOP_TEST_COUNTER"
  printf '%s\n' '{"type":"result","subtype":"success","is_error":true,"num_turns":4,"total_cost_usd":0.5,"duration_ms":1000,"duration_api_ms":900,"result":"API Error: Connection closed mid-response. The response above may be incomplete.","usage":{"input_tokens":10,"output_tokens":5}}'
  exit 1
fi
printf '{"type":"system","subtype":"init","model":"resolved-opus-test"}\n'
exit 0
"""

_FAKE_GH = "#!/usr/bin/env bash\nexit 1\n"  # no PRs / gh unavailable — loop fail-softs on it

_SANDBOX_TODOS = """# TODOS

### A ready ticket the fake agent never strikes
**Priority:** P4
**Why:** the loop must re-pick this every pass so the spin-guard's ERROR arm is exercised.
"""

_SCRIPT_LINKS = [
    "loop_next_todo.sh",
    "loop_stats.py",
    "next_todo.py",
    "loop_next_todo.settings.json",
    "loop_stop_guard.sh",
]


def _git(sandbox: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(sandbox), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _build_sandbox(tmp_path: Path) -> Path:
    """A throwaway git repo whose scripts/ symlinks the REAL loop scripts, so
    `cd "$ROOT"` inside loop_next_todo.sh lands here — never the real value-hunt repo."""
    sandbox = tmp_path / "sandbox"
    (sandbox / "scripts").mkdir(parents=True)
    for name in _SCRIPT_LINKS:
        (sandbox / "scripts" / name).symlink_to(ROOT / "scripts" / name)
    (sandbox / "TODOS.md").write_text(_SANDBOX_TODOS, encoding="utf-8")
    _git(sandbox, "init", "-q")
    _git(sandbox, "config", "user.email", "t@e.st")
    _git(sandbox, "config", "user.name", "test")
    _git(sandbox, "add", "-A")
    _git(sandbox, "commit", "-q", "-m", "seed")
    _git(sandbox, "branch", "-M", "main")
    return sandbox


def _run_drain(tmp_path: Path, error_retries: int) -> tuple[subprocess.CompletedProcess[str], int]:
    sandbox = _build_sandbox(tmp_path)
    counter = tmp_path / "counter"
    fakebin = _make_fake_bin(tmp_path, "claude", _FAKE_CLAUDE_ERR)
    _make_fake_bin(tmp_path, "gh", _FAKE_GH)  # same fakebin dir

    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOP_")}
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
    env["LOOP_TEST_COUNTER"] = str(counter)
    env["LOOP_ERROR_RETRIES"] = str(error_retries)
    env["LOOP_ERROR_RETRY_BACKOFF_SECS"] = "0"  # keep the test instant
    env["LOOP_HEARTBEAT_SECS"] = "0"
    env["LOOP_NO_CAFFEINATE"] = "1"
    env["LOOP_MAX_ITERS"] = "12"  # safety net: an infinite-retry bug hits this and exits 0, not 3
    env["LOOP_STATS_DIR"] = str(tmp_path / "stats")  # OUTSIDE the sandbox repo (gitignore guard)

    proc = subprocess.run(
        ["bash", str(sandbox / "scripts" / "loop_next_todo.sh")],
        cwd=sandbox,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    attempts = int(counter.read_text().strip()) if counter.exists() else 0
    return proc, attempts


def test_transient_error_retries_then_spin_stops() -> None:
    """The core wiring: with LOOP_ERROR_RETRIES=2 a pick that errors every pass is attempted
    exactly 3 times (1 initial + 2 retries), then the loop exits 3 with the sustained-outage
    message — NOT the generic 'inspect, then re-run', and NOT an infinite loop (would hit
    MAX_ITERS=12 and exit 0)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        proc, attempts = _run_drain(Path(d), error_retries=2)
    combined = proc.stdout + proc.stderr
    assert attempts == 3, f"expected 3 attempts (1+2), got {attempts}\n{combined}"
    assert proc.returncode == 3, f"expected spin-stop exit 3, got {proc.returncode}\n{combined}"
    assert "Repeated transient error" in combined
    assert "Retrying the SAME item (attempt 1 of 2" in combined
    assert "Retrying the SAME item (attempt 2 of 2" in combined
    # It must NOT fall through to the generic no-land message.
    assert "stopping to avoid a spin loop" not in combined


def test_error_retries_zero_stops_on_first_error() -> None:
    """LOOP_ERROR_RETRIES=0 restores the pre-retry behaviour: attempted exactly once, then stop."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        proc, attempts = _run_drain(Path(d), error_retries=0)
    combined = proc.stdout + proc.stderr
    assert attempts == 1, f"expected exactly 1 attempt, got {attempts}\n{combined}"
    assert proc.returncode == 3, f"expected exit 3, got {proc.returncode}\n{combined}"
    assert "Repeated transient error" in combined
