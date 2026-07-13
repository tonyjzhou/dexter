"""Regression oracle for the LOOP_MODEL family knob in scripts/loop_next_todo.sh.

Contract (SPEC-derived from the request that motivated it, NOT read off the implementation):

  - LOOP_MODEL unset            -> defaults to "opus"
  - LOOP_MODEL=opus|sonnet      -> case-insensitive; resolves to the bare CLI alias
                                    (`--model opus` / `--model sonnet`), which Claude Code keeps
                                    pointed at that family's most advanced released model — no
                                    version pin needed here.
  - LOOP_MODEL=<anything else>  -> hard error, exit non-zero, message names both valid choices
  - Effort defaults to "max"    -> regardless of which valid family is chosen (both honor a headless
                                    --effort flag), unless LOOP_EFFORT explicitly overrides it.
  - The thoroughness banner label reflects whichever valid family was chosen — it is not hardcoded
    to require a particular model to call a run the stock drain.
  - Async workflows are DISABLED by default (the script exports CLAUDE_CODE_DISABLE_WORKFLOWS=1):
    the async Workflow tool strands work in a one-shot headless pass, so the stock headless drain is
    max effort + SYNCHRONOUS orchestration, and the banner says "max synchronous (… async
    workflows off)". An explicitly-empty CLAUDE_CODE_DISABLE_WORKFLOWS re-enables them (banner reverts
    to the "max + async workflows ON" label + a strand-risk warning) — `-` not `:-` expansion, so
    empty stays empty. (The former `ultracode: true` settings key was removed as vestigial: with
    workflows off it had no effect; effort is carried entirely by the `--effort` flag.)
  - The "model" banner line shows the EXACT resolved model version (e.g. "claude-opus-4-8"), not
    just the family alias — resolved via a live `claude` probe (resolve_model_version) that fails
    soft to the bare alias on any error.

Drives the REAL script end-to-end via `LOOP_DRY_RUN=1` (no `claude -p /next-todo` pass runs, no
git mutation beyond a read-only `next_todo.py --json` call) rather than extracting a function
fragment — this knob is read in multiple places (precondition validation, claude_args assembly,
the print_run_config banner) so a full-script run is the only way to pin the seam between them.

A FAKE `claude` is always placed at the front of PATH (mirroring the established fake-external-
binary pattern in test_loop_new_prs_json.py, there for `gh`) so resolve_model_version's live probe
never makes a real network call: it stays instant, free, deterministic, and offline-capable.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "loop_next_todo.sh"

# Echoes back a stream-json-shaped init line carrying a model id derived from the `--model` value
# it was invoked with, so a test can assert resolve_model_version threads the family through
# correctly. Mirrors the real CLI's first stream-json event closely enough for the python parser
# in resolve_model_version (`json.load(...).get("model", "")`) to behave identically.
_FAKE_CLAUDE_OK = """#!/usr/bin/env bash
model=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) model="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '{"type":"system","subtype":"init","model":"resolved-%s-test"}\\n' "$model"
"""

# Exists (so `command -v claude` still finds it) but fails on every invocation, simulating
# offline / no-auth / a hung CLI start — exercises resolve_model_version's fail-soft path.
_FAKE_CLAUDE_BROKEN = """#!/usr/bin/env bash
exit 1
"""


def _make_fake_claude(tmp_path: Path, *, broken: bool = False) -> Path:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    claude = fakebin / "claude"
    claude.write_text(_FAKE_CLAUDE_BROKEN if broken else _FAKE_CLAUDE_OK)
    claude.chmod(0o755)
    return fakebin


def _run(
    env_overrides: dict[str, str],
    tmp_path: Path,
    *,
    fake_claude_broken: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the real script with LOOP_DRY_RUN=1 and the given env overrides layered on a clean
    copy of the ambient environment (so a developer's own LOOP_* exports can't leak into a test),
    with the fake `claude` prepended to PATH ahead of everything else (including a real `claude`,
    if any) so resolve_model_version's probe is always the fake, never a live network call."""
    fakebin = _make_fake_claude(tmp_path, broken=fake_claude_broken)
    # Strip LOOP_* AND CLAUDE_CODE_DISABLE_WORKFLOWS from the base env so a developer's own exports
    # can't leak into a test — the async-workflow default must be exercised from a genuinely-unset var
    # (tests that need it re-enabled pass it explicitly via env_overrides).
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("LOOP_") and k != "CLAUDE_CODE_DISABLE_WORKFLOWS"
    }
    env["LOOP_DRY_RUN"] = "1"
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _field(stdout: str, label: str) -> str:
    """Extract the value of a "  <label>...: <value>" banner line."""
    m = re.search(rf"^  {re.escape(label)}\s*: (.+)$", stdout, re.MULTILINE)
    assert m, f"banner line for {label!r} not found in:\n{stdout}"
    return m.group(1).strip()


def test_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_default_model_is_opus(tmp_path: Path) -> None:
    result = _run({}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "model") == "resolved-opus-test (alias: opus)"
    assert _field(result.stdout, "effort") == "max"
    assert "--model opus --effort max" in result.stdout
    assert "max synchronous (Opus max effort" in result.stdout


def test_async_workflows_disabled_by_default(tmp_path: Path) -> None:
    """A headless drain must NOT expose the async `Workflow` tool. It returns immediately with a
    task id and re-invokes the agent on a completion callback a one-shot `claude -p` NEVER receives,
    so an agent that yields to a workflow ("kicked off a workflow… will report back") strands its
    already-applied edits uncommitted (run 20260701T114041Z pass 22 lost a real, tested fix this
    way). The script exports CLAUDE_CODE_DISABLE_WORKFLOWS=1 by default; the banner must report async
    workflows OFF + synchronous orchestration, and MUST NOT show the async-orchestration label.
    Spec-derived from the incident + the chosen fix (disable async workflows), not read off code."""
    result = _run({}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "thoroughness") == (
        "max synchronous (Opus max effort · Task/Agent + /fr, async workflows off)"
    )
    assert "async workflows ON" not in result.stdout


def test_async_workflows_reenabled_only_by_explicitly_empty_var(tmp_path: Path) -> None:
    """The escape hatch: an explicitly-EMPTY CLAUDE_CODE_DISABLE_WORKFLOWS re-enables async
    workflows — the banner reverts to the async-orchestration label with a strand-risk warning.
    This pins the `-` (not `:-`) parameter expansion: `:-` would coerce empty back to the "1"
    default and make the documented empty-var re-enable a silent no-op."""
    result = _run({"CLAUDE_CODE_DISABLE_WORKFLOWS": ""}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    thoroughness = _field(result.stdout, "thoroughness")
    assert thoroughness.startswith("max + async workflows ON (Opus)")
    assert "can strand work" in thoroughness


@pytest.mark.parametrize("value", ["opus", "OPUS", "Opus"])
def test_loop_model_opus_case_insensitive(value: str, tmp_path: Path) -> None:
    result = _run({"LOOP_MODEL": value}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "model") == "resolved-opus-test (alias: opus)"
    assert _field(result.stdout, "effort") == "max"
    assert "--model opus --effort max" in result.stdout
    assert "max synchronous (Opus max effort" in result.stdout


@pytest.mark.parametrize("value", ["sonnet", "SONNET", "Sonnet"])
def test_loop_model_sonnet_case_insensitive(value: str, tmp_path: Path) -> None:
    result = _run({"LOOP_MODEL": value}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "model") == "resolved-sonnet-test (alias: sonnet)"
    assert "--model sonnet --effort max" in result.stdout
    assert "max synchronous (Sonnet max effort" in result.stdout


@pytest.mark.parametrize("value", ["haiku", "claude-opus-4-8", "gpt-4"])
def test_loop_model_invalid_value_is_a_hard_error(value: str, tmp_path: Path) -> None:
    result = _run({"LOOP_MODEL": value}, tmp_path)
    assert result.returncode != 0
    assert "opus" in result.stdout and "sonnet" in result.stdout
    assert "ERROR" in result.stdout


def test_loop_model_empty_string_defaults_like_unset(tmp_path: Path) -> None:
    """Empty string is treated the same as unset by bash `:-` defaulting, so it resolves to the
    default ("opus") rather than erroring — only a genuinely non-empty invalid value errors."""
    result = _run({"LOOP_MODEL": ""}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "model") == "resolved-opus-test (alias: opus)"


def test_loop_effort_override_breaks_stock_ultracode_label_regardless_of_model(
    tmp_path: Path,
) -> None:
    """A non-max LOOP_EFFORT is a real override and must say so — for EITHER valid model
    family, not just the (former) opus-only special case."""
    result = _run({"LOOP_MODEL": "sonnet", "LOOP_EFFORT": "high"}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "model") == "resolved-sonnet-test (alias: sonnet)"
    assert _field(result.stdout, "effort") == "high"
    assert "custom override" in result.stdout
    assert "async_workflows=off" in result.stdout

    result = _run({"LOOP_MODEL": "opus", "LOOP_EFFORT": "high"}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "model") == "resolved-opus-test (alias: opus)"
    assert "custom override" in result.stdout
    assert "async_workflows=off" in result.stdout


def test_resolved_model_version_differs_from_bare_alias(tmp_path: Path) -> None:
    """The whole point of the feature: the printed model line carries the CONCRETE resolved
    version, not merely a restatement of the family alias the user set."""
    result = _run({"LOOP_MODEL": "opus"}, tmp_path)
    model_field = _field(result.stdout, "model")
    assert model_field != "opus"
    assert "resolved-opus-test" in model_field
    assert "alias: opus" in model_field


def test_resolve_model_version_fails_soft_when_claude_probe_fails(tmp_path: Path) -> None:
    """A broken/unreachable claude (offline, no auth, hung CLI start) must never abort the
    banner or the script — `set -euo pipefail` is in play, so this also pins the `|| true`
    guard around the probe's pipeline. The model line degrades to the bare alias with an
    honest note, and the script still exits 0 (this is a dry run; nothing destructive happens)."""
    result = _run({"LOOP_MODEL": "sonnet"}, tmp_path, fake_claude_broken=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        _field(result.stdout, "model")
        == "sonnet (exact version unresolved — claude probe failed or timed out)"
    )
