"""Regression oracle for the `new_prs_json` shell function in scripts/loop_next_todo.sh.

`new_prs_json` is the per-pass "what PRs did this loop pass open?" probe. Its contract
(SPEC-derived from the function's documented intent, NOT read off the implementation):

  - gh UP, numeric $since      -> JSON array of @me PRs on a claude/* branch with number > $since
  - gh UP, no matching PRs     -> "[]"
  - gh UP, empty/non-numeric   -> "[]" (no baseline -> can't filter without mislabeling; gh confirmed up)
  - gh DOWN (non-zero exit)    -> {"gh_error": true}  (a reporting gap, NOT a definitive no-PR)
  - gh DOWN, empty $since       -> {"gh_error": true}  (the SUSTAINED-outage case: gh is probed BEFORE
                                  the empty-$since short-circuit, so pr_before="" still surfaces the gap)

The last case is the one a naive implementation gets wrong: when gh is already down at
pr_max() time, $since is "", and short-circuiting on it before calling gh would return
"[]" and silently read as "no new PRs" — the exact conflation the sentinel exists to kill.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "loop_next_todo.sh"

# A non-empty gh stdout with one new claude/* PR (#225) and one older non-claude PR (#201).
_GH_BODY = (
    '[{"number":225,"title":"x","state":"OPEN","url":"u","headRefName":"claude/foo"},'
    '{"number":201,"title":"old","state":"MERGED","url":"u","headRefName":"feature/bar"}]'
)


def _extract_fn() -> str:
    """Pull just the new_prs_json() definition out of the loop script (it has no standalone
    entrypoint; sourcing the whole script would run the loop)."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^new_prs_json\(\) \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    assert m, "new_prs_json() not found in loop_next_todo.sh"
    return m.group(0)


def _run(since: str, *, gh_body: str | None, gh_rc: int, tmp_path: Path) -> str:
    """Invoke new_prs_json with a fake `gh` on PATH that prints gh_body and exits gh_rc."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    gh = fakebin / "gh"
    body = "" if gh_body is None else gh_body
    gh.write_text(f"#!/usr/bin/env bash\nprintf '%s' {_sh_quote(body)}\nexit {gh_rc}\n")
    gh.chmod(0o755)

    harness = f"set -euo pipefail\n{_extract_fn()}\nnew_prs_json {_sh_quote(since)}\n"
    env = {"PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(tmp_path)}
    out = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip()


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def test_gh_up_filters_to_new_claude_prs(tmp_path: Path) -> None:
    out = _run("200", gh_body=_GH_BODY, gh_rc=0, tmp_path=tmp_path)
    # Only #225 (number>200 AND claude/* branch); #201 dropped (non-claude); headRefName projected out.
    assert out == '[{"number": 225, "title": "x", "state": "OPEN", "url": "u"}]'


def test_gh_up_no_matches_is_empty_array(tmp_path: Path) -> None:
    out = _run("200", gh_body="[]", gh_rc=0, tmp_path=tmp_path)
    assert out == "[]"


def test_gh_up_empty_since_is_empty_array_not_sentinel(tmp_path: Path) -> None:
    """No numeric baseline but gh is reachable -> honest "[]" (NOT the gh-error sentinel)."""
    out = _run("", gh_body="[]", gh_rc=0, tmp_path=tmp_path)
    assert out == "[]"


def test_gh_down_numeric_since_is_sentinel(tmp_path: Path) -> None:
    out = _run("200", gh_body=None, gh_rc=1, tmp_path=tmp_path)
    assert out == '{"gh_error": true}'


def test_gh_down_empty_since_is_sentinel_not_empty_array(tmp_path: Path) -> None:
    """THE regression: a SUSTAINED outage makes pr_before="" — gh is probed BEFORE the empty-$since
    short-circuit, so the gap surfaces as the sentinel instead of a false "[]" (no-PRs)."""
    out = _run("", gh_body=None, gh_rc=1, tmp_path=tmp_path)
    assert out == '{"gh_error": true}'


@pytest.mark.parametrize("body", ["", "not json", '{"message":"boom"}'])
def test_gh_up_unparseable_body_is_sentinel(body: str, tmp_path: Path) -> None:
    """gh exits 0 but its body can't be filtered (empty / malformed / a non-array error envelope) ->
    the python filter fails and the sentinel is emitted, never a misleading "[]"."""
    out = _run("200", gh_body=body, gh_rc=0, tmp_path=tmp_path)
    assert out == '{"gh_error": true}'
