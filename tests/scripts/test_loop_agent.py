"""Regression oracle for LOOP_AGENT=claude|grok dual-CLI support in loop_next_todo.sh.

Contract (SPEC-derived — agent-agnostic drain harness, not read off the implementation):

  - LOOP_AGENT unset / claude  → default Claude path (byte-stable with pre-dual-agent behaviour):
      requires `claude` on PATH, LOOP_MODEL closed opus|sonnet (default opus), settings file,
      --max-budget-usd, --permission-mode auto | --dangerously-skip-permissions.
  - LOOP_AGENT=grok            → Grok path: requires `grok` on PATH, LOOP_MODEL freeform
      (default grok-4.5), --yolo, no settings / no max-budget-usd, optional LOOP_MAX_TURNS.
  - LOOP_AGENT=<other>         → hard error naming both valid choices.
  - Switching agents is env-only — same script, same TODOS.md / spin-guard / stats spine.
  - Dry-run prints `agent : …` and `cmd : <bin> …` for the selected agent; never invokes
      a real /next-todo pass.

Drives the REAL script via LOOP_DRY_RUN=1 with fake claude/grok on PATH (no network).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "loop_next_todo.sh"

_FAKE_CLAUDE = """#!/usr/bin/env bash
model=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model) model="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '{"type":"system","subtype":"init","model":"resolved-%s-test"}\\n' "$model"
"""

_FAKE_GROK = """#!/usr/bin/env bash
# Dry-run never invokes grok for a real pass; resolve_model_version is Claude-only.
# Still exit 0 if accidentally called.
exit 0
"""


def _make_fakes(tmp_path: Path, *, with_claude: bool = True, with_grok: bool = True) -> Path:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    if with_claude:
        p = fakebin / "claude"
        p.write_text(_FAKE_CLAUDE)
        p.chmod(0o755)
    if with_grok:
        p = fakebin / "grok"
        p.write_text(_FAKE_GROK)
        p.chmod(0o755)
    return fakebin


def _run(
    env_overrides: dict[str, str],
    tmp_path: Path,
    *,
    with_claude: bool = True,
    with_grok: bool = True,
) -> subprocess.CompletedProcess[str]:
    fakebin = _make_fakes(tmp_path, with_claude=with_claude, with_grok=with_grok)
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
    m = re.search(rf"^  {re.escape(label)}\s*: (.+)$", stdout, re.MULTILINE)
    assert m, f"banner line for {label!r} not found in:\n{stdout}"
    return m.group(1).strip()


def test_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_default_agent_is_claude(tmp_path: Path) -> None:
    result = _run({}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "agent") == "claude"
    assert "cmd : claude " in result.stdout
    assert "--model opus --effort max" in result.stdout
    assert "--max-budget-usd" in result.stdout
    assert "--settings" in result.stdout


@pytest.mark.parametrize("value", ["claude", "CLAUDE", "Claude"])
def test_loop_agent_claude_case_insensitive(value: str, tmp_path: Path) -> None:
    result = _run({"LOOP_AGENT": value}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "agent") == "claude"
    assert "cmd : claude " in result.stdout


@pytest.mark.parametrize("value", ["grok", "GROK", "Grok"])
def test_loop_agent_grok_case_insensitive(value: str, tmp_path: Path) -> None:
    result = _run({"LOOP_AGENT": value}, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "agent") == "grok"
    assert "cmd : grok " in result.stdout
    assert "-m grok-4.5" in result.stdout or "-m " in result.stdout
    assert "--effort max" in result.stdout
    assert "--yolo" in result.stdout
    assert "--output-format json" in result.stdout
    # Claude-only flags must NOT appear on the Grok command line.
    assert "--max-budget-usd" not in result.stdout.split("cmd :", 1)[-1]
    assert "--settings" not in result.stdout.split("cmd :", 1)[-1]
    assert "--dangerously-skip-permissions" not in result.stdout.split("cmd :", 1)[-1]
    assert _field(result.stdout, "model") == "grok-4.5"
    assert "n/a on Grok" in _field(result.stdout, "budget")


def test_grok_custom_model_and_max_turns(tmp_path: Path) -> None:
    result = _run(
        {
            "LOOP_AGENT": "grok",
            "LOOP_MODEL": "grok-composer-2.5-fast",
            "LOOP_MAX_TURNS": "80",
            "LOOP_EFFORT": "high",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "agent") == "grok"
    assert _field(result.stdout, "model") == "grok-composer-2.5-fast"
    assert _field(result.stdout, "effort") == "high"
    assert _field(result.stdout, "max-turns") == "80"
    cmd = result.stdout.split("cmd :", 1)[-1]
    assert "-m grok-composer-2.5-fast" in cmd
    assert "--max-turns 80" in cmd
    assert "--effort high" in cmd


def test_invalid_agent_is_hard_error(tmp_path: Path) -> None:
    result = _run({"LOOP_AGENT": "codex"}, tmp_path)
    assert result.returncode != 0
    assert "ERROR" in result.stdout
    assert "claude" in result.stdout and "grok" in result.stdout


def _run_isolated_path(
    env_overrides: dict[str, str],
    tmp_path: Path,
    *,
    with_claude: bool,
    with_grok: bool,
) -> subprocess.CompletedProcess[str]:
    """Like `_run`, but PATH is ONLY the fake bin + a minimal system dir so a
    real `claude`/`grok` on the developer machine cannot satisfy the precondition.
    python3/git still come from a real system path for the dry-run selector."""
    fakebin = _make_fakes(tmp_path, with_claude=with_claude, with_grok=with_grok)
    # Keep /usr/bin + /bin so python3/git/bash exist; do NOT include ~/.local or
    # Homebrew paths where the real agent CLIs usually live.
    minimal = f"{fakebin}:/usr/bin:/bin"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("LOOP_") and k != "CLAUDE_CODE_DISABLE_WORKFLOWS"
    }
    env["LOOP_DRY_RUN"] = "1"
    env["PATH"] = minimal
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_grok_requires_grok_on_path(tmp_path: Path) -> None:
    result = _run_isolated_path(
        {"LOOP_AGENT": "grok"}, tmp_path, with_claude=True, with_grok=False
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "grok" in combined.lower()
    assert "PATH" in combined or "path" in combined.lower()


def test_claude_still_requires_claude_on_path(tmp_path: Path) -> None:
    result = _run_isolated_path(
        {"LOOP_AGENT": "claude"}, tmp_path, with_claude=False, with_grok=True
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "claude" in combined.lower()


def test_switch_agent_does_not_require_the_other_cli(tmp_path: Path) -> None:
    """Grok drain must not require `claude` on PATH (and vice versa for the default)."""
    result = _run_isolated_path(
        {"LOOP_AGENT": "grok"}, tmp_path, with_claude=False, with_grok=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _field(result.stdout, "agent") == "grok"
