#!/usr/bin/env bash
# Dexter — drain TODOS.md by running /next-todo once per item, EACH IN A FRESH CONTEXT.
#
# Agent CLI (LOOP_AGENT=claude|grok, default claude): each pass is a one-shot headless session of
# either `claude -p` or `grok -p`. Switch agents between drains; the selector, spin-guard, auto-park,
# and stats spine are agent-agnostic. Claude is the production default (full budget + Stop-hook
# prevention); Grok is a first-class alternative with the gaps documented under LOOP_AGENT below.
#
# Why this instead of `/loop /next-todo`: the built-in /loop keeps every iteration in ONE
# accumulating context (and /clear cannot be embedded inside it). /next-todo persists all of its
# state to disk + git — it strikes the item in TODOS.md and commits directly to main on each pass — so a
# brand-new session per item loses nothing. And every headless `-p` invocation IS a fresh session:
# that fresh session is exactly the "/clear between every /next-todo" you want, with no context
# bloat and no cross-item bleed.
#
# Each pass:  sync local main -> select the next READY item (deterministic) -> run ONE headless
# /next-todo -> record stats -> repeat. Stops cleanly when no READY item is left, and bails
# (exit 3) on a genuine spin: the selector re-picks the SAME item AND the last pass landed no commit
# on origin/main. A repeat pick alone is NOT a spin — the agent's own /next-todo applies judgment and
# may work a DIFFERENT item (e.g. when the selector's #1 carries a PROSE-only deferral the deterministic
# selector can't see), so the backlog keeps draining while that pick stays #1; the loop continues and
# advises parking it. (Pushing a commit to origin/main is the AUTHORITATIVE land signal — not the
# brittle closed-count, which misses a strike that drops the item's **Priority:** line.)
#
# VISIBILITY (so a working pass is never mistaken for a hung one):
#   * A background HEARTBEAT prints "still working … Nm elapsed · branch X" every LOOP_HEARTBEAT_SECS
#     (default 60) in quiet mode — under direct-to-main the branch normally stays `main` throughout
#     (the pass builds AND commits on main). Set LOOP_VERBOSE=1 to instead stream the agent turn-by-turn.
#   * After each pass a one-line STATS read prints: wall-clock, turns, cost, tokens, permission
#     denials, landed?/PR — so progress is legible without --verbose.
#   * After each pass the loop reports what the agent ACTUALLY did (PRs it opened/merged), which can
#     differ from the printed pick when the loop's local TODOS.md lagged the agent's own sync.
#   * On every exit (drain / max-iters / spin-stop / Ctrl-C / crash) a SUMMARY of all passes prints.
#   Stats persist as JSONL under LOOP_STATS_DIR (default $ROOT/.loop-runs, gitignored).
#   Cross-RUN view: `python3 scripts/loop_stats.py history` aggregates every run file — outcome
#   totals, cost per run, and the repeat offenders (items that keep not landing across runs).
#
# KEEP-AWAKE (macOS): a long unattended overnight drain dies the moment the laptop idle-sleeps —
#   the `claude` process is suspended and the network drops mid-pass. The loop therefore holds a
#   `caffeinate` power assertion for EXACTLY its own lifetime (`caffeinate -i -m -s -w $$`, which
#   waits on this script's PID and self-exits when the loop ends; cleanup() also kills it). Opt out
#   with LOOP_NO_CAFFEINATE=1. Off macOS, or when `caffeinate` is absent, it degrades to a silent
#   no-op and the loop runs unchanged. CAVEAT: caffeinate stops *idle* sleep and (on AC power)
#   *system* sleep, so assume the Mac is plugged in with the lid open — or in clamshell with an
#   external display/keyboard. On battery with the lid closed, macOS clamshell-sleeps regardless.
#
# Run from anywhere — it locates the repo root from its own path and operates on `main`.
# Run it from a CLEAN working tree; this loop autonomously commits and pushes DIRECTLY to main (solo
# dev — no PR/CI: each /next-todo pass commits the strike + VERSION/CHANGELOG straight to main).
#
# MODEL + EFFORT: every pass runs a "max synchronous" drain by default — the latest Opus at
# max effort with async workflows DISABLED (they strand work in a one-shot headless pass; see the
# CLAUDE_CODE_DISABLE_WORKFLOWS guard below). BECAUSE async workflows are off there is no cheap
# parallel fan-out to lean on, so per-pass thoroughness has to come from the model itself: one
# powerful model reasoning hard, synchronously (Task/Agent + /fr orchestration that commits
# in-turn), under --permission-mode auto. That is why the default is Opus at max effort rather than
# the cheaper Sonnet/xhigh split — with the fan-out lever removed, capability per pass is the whole
# game and the higher spend is the deliberate trade (hence the raised default budget below). Choose
# the model FAMILY with LOOP_MODEL=opus|sonnet (default: opus; case-insensitive, any other value is
# a hard error) — each resolves to the bare CLI alias (`--model opus` / `--model sonnet`), which
# Claude Code always keeps pointed at that family's most advanced released model, so this script
# never needs a version bump when a new model ships. Both families honor a headless `--effort` flag
# as of CLI 2.1.197 (xhigh verified 2026-06-30; max is the higher tier and the new default) —
# override the effort itself via LOOP_EFFORT below.
#
# Config (env vars, all optional):
#   LOOP_AGENT=claude|grok   headless CLI for each pass             (default: claude)
#                             Switch anytime between drains — same harness, same TODOS.md spin-guard
#                             / auto-park / stats spine. Claude path is byte-stable when unset.
#                             Grok notes: no --max-budget-usd (use pass-timeout / LOOP_MAX_TURNS);
#                             Stop hooks are passive on Grok (dirty-tree + auto-park still catch
#                             abandoned/stalled yields); unattended always uses --yolo (Grok has no
#                             Claude --settings allowlist load path for this loop file).
#   LOOP_MAX_ITERS=N          hard cap on iterations              (default 0 = run until drained)
#   LOOP_MAX_BUDGET_USD=N     per-iteration API spend cap         (default 150 for claude; set EMPTY
#                             to disable). CLAUDE ONLY — Grok has no spend-cap flag; ignored there
#                             (banner says so). NOTE: a /next-todo pass is a full build->test->
#                             review->commit-to-main vertical and Opus at max effort spends
#                             substantially more than the old Sonnet/xhigh default, so the cap was
#                             raised from 50 to 150 to keep a large item from aborting half-done.
#   LOOP_MAX_RUN_BUDGET_USD=N advisory cap on the RUN's total recorded spend (default empty = off;
#                             decimals OK). Checked after each pass; over → clean stop before the
#                             next pick. Only meaningful when passes report cost_usd (Claude);
#                             Grok passes typically have no cost → aggregate stays $0 / never trips.
#   LOOP_MAX_TURNS=N          Grok-only: pass --max-turns N (default empty = off). Cheap runaway
#                             guard when USD budgeting is unavailable.
#   LOOP_SLEEP=DURATION       pause between iterations             (default 0; e.g. 30, 2m — `sleep` arg)
#   LOOP_MODEL=…              model for --model / -m              (default: opus for claude,
#                             grok-4.5 for grok). Claude: closed opus|sonnet family aliases.
#                             Grok: any non-empty model id the `grok` CLI accepts.
#   LOOP_EFFORT=level         --effort passed to the agent         (default: max; low|medium|high|xhigh|max)
#   LOOP_VERBOSE=1            stream each pass turn-by-turn instead of the heartbeat.
#   LOOP_HEARTBEAT_SECS=N     liveness cadence in quiet mode       (default 60; 0 disables)
#   LOOP_STATS_DIR=PATH       where per-run stats JSONL is written (default $ROOT/.loop-runs).
#                             If inside the repo it MUST be git-ignored (else its writes dirty the
#                             tree and main-sync skips forever) — the loop refuses to start otherwise.
#   LOOP_PASS_TIMEOUT_SECS=N  kill a single pass after N seconds   (default 14400 = 4h; 0 disables;
#                             needs `timeout`/`gtimeout`). A wedge-only backstop, deliberately ABOVE
#                             the budget-cap wall-clock envelope (~3.5h at the hottest observed burn
#                             against the $150 cap; all-time max real pass 5910s) — the budget cap
#                             still fires first on any spend-active Claude pass, so the watchdog only
#                             reaps a hung zero-spend agent process that would otherwise stall an
#                             unattended drain forever. A timeout kill (rc=124) rides the ERROR-retry
#                             arm below: ≤(retries+1)×ceiling, then a clean spin-stop.
#   LOOP_ERROR_RETRIES=N      retry the SAME pick after a transient hard error (default 2; 0 disables:
#                             a transient rc≠0 pass spin-stops at once, the pre-retry behaviour). An
#                             rc≠0/is_error pass (e.g. "API Error: Connection closed mid-response")
#                             means the item was NEVER attempted — the agent died mid-orientation, not
#                             on a genuine no-progress decision — so a one-off network blip costs one
#                             retry (fresh headless session = fresh connection), not the whole ready backlog.
#   LOOP_ERROR_RETRY_BACKOFF_SECS=N  pause before each such retry (default 10) so a brief blip can clear.
#   LOOP_NO_CAFFEINATE=1      disable the macOS keep-awake assertion (let the machine sleep normally).
#   LOOP_KEEPALIVE_OS=NAME    override the detected OS for the keep-awake plan (default `uname -s`);
#                             set to a non-Darwin value to preview the off-macOS no-op anywhere.
#   LOOP_BYPASS_PERMISSIONS=1 Claude: swap the curated allowlist for --dangerously-skip-permissions.
#                             Grok: no-op (Grok path always --yolo for unattended). SANDBOX ONLY —
#                             this loop commits and pushes DIRECTLY to main.
#   LOOP_DRY_RUN=1            print the next pick + the exact agent command, then exit. No
#                             /next-todo pass runs; on Claude the banner does ONE cheap local probe
#                             (cut off before any completion — see resolve_model_version) to show
#                             the exact resolved model version, not just the family alias.
#
# Auth: Claude inherits your interactive Claude Code login (CLAUDE_CODE_OAUTH_TOKEN for unattended).
# Grok inherits your interactive `grok login` session.
#
# Permissions (Claude): by default each pass runs under --permission-mode auto with the curated
# allowlist in scripts/loop_next_todo.settings.json — the dexter dev toolchain (git/gh/make/
# bun/bunx/node/npx/tsx + safe shell builtins) is pre-approved and anything outside it is decided
# by auto mode's "safe yellow" judgment unattended rather than blocking on a prompt; secrets, sudo,
# curl/wget, rm -rf, the WhatsApp session store (./.dexter/credentials — Baileys creds.json),
# package publishes (npm/bun publish), and destructive force-push (--force / -f) + hard-reset stay denied — but the SAFE
# --force-with-lease IS allowed (a vestigial safety net from before the direct-to-main switch —
# harmless now that each pass commits straight to main and never creates a branch). The deny rules
# are best-effort defense-in-depth, NOT a security boundary (Bash patterns are bypassable). The real
# boundary is the environment you run this in: this loop commits to main on its own, so run it only
# where you are comfortable letting it do that.
# Permissions (Grok): always --yolo (required for unattended; no Claude --settings allowlist path).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SETTINGS="$ROOT/scripts/loop_next_todo.settings.json"
MAX_ITERS="${LOOP_MAX_ITERS:-0}"
# Default $150/pass runaway guard (Opus max-effort passes — deep reasoning + synchronous Task/Agent +
# /fr review — spend several times a Sonnet/xhigh pass; raised from 50 so a large item isn't aborted
# half-done). Default applies only when UNSET, so `LOOP_MAX_BUDGET_USD=` (empty) disables it.
MAX_BUDGET="${LOOP_MAX_BUDGET_USD-150}"
RUN_BUDGET="${LOOP_MAX_RUN_BUDGET_USD:-}"   # run-level advisory aggregate; empty = off
SLEEP_FOR="${LOOP_SLEEP:-0}"
HEARTBEAT_SECS="${LOOP_HEARTBEAT_SECS:-60}"
STATS_DIR="${LOOP_STATS_DIR:-$ROOT/.loop-runs}"
PASS_TIMEOUT="${LOOP_PASS_TIMEOUT_SECS:-14400}"
# A transient hard pass error (rc≠0 / is_error — e.g. an "API Error: Connection closed mid-response"
# stream drop) leaves the item NEVER adjudicated, so retry the SAME pick up to ERROR_RETRIES times
# (fresh `claude -p` = fresh connection) before spin-stopping; only that many CONSECUTIVE errors on
# one pick — a sustained outage — halts. 0 restores the old behaviour (a transient error spin-stops
# at once). A short backoff lets a brief blip clear before the retry.
ERROR_RETRIES="${LOOP_ERROR_RETRIES:-2}"
ERROR_BACKOFF_SECS="${LOOP_ERROR_RETRY_BACKOFF_SECS:-10}"
NO_CAFFEINATE="${LOOP_NO_CAFFEINATE:-0}"
# `|| echo unknown` keeps a (near-impossible) `uname` failure from aborting the script under set -e;
# an unknown OS is non-Darwin, so it correctly takes the keep-awake "skipped" no-op path.
KEEPALIVE_OS="${LOOP_KEEPALIVE_OS:-$(uname -s 2>/dev/null || echo unknown)}"
# Resolve a relative LOOP_STATS_DIR against the repo root (cwd is $ROOT) so the
# inside-repo guard below sees a real absolute path, not a cwd-relative one.
case "$STATS_DIR" in /*) ;; *) STATS_DIR="$ROOT/$STATS_DIR" ;; esac

# --- preconditions (fail fast) ---
# Agent CLI — closed two-value choice. Default claude keeps every existing invocation byte-stable.
# `tr` (not bash's `${var,,}`) for the lowercase fold — macOS ships bash 3.2.
LOOP_AGENT="$(printf '%s' "${LOOP_AGENT:-claude}" | tr '[:upper:]' '[:lower:]')"
case "$LOOP_AGENT" in
  claude|grok) ;;
  *) echo "ERROR: LOOP_AGENT must be 'claude' or 'grok' (got '$LOOP_AGENT')"; exit 1 ;;
esac
case "$LOOP_AGENT" in
  claude) command -v claude >/dev/null 2>&1 || { echo "ERROR: 'claude' CLI not on PATH (LOOP_AGENT=claude)"; exit 1; } ;;
  grok)   command -v grok   >/dev/null 2>&1 || { echo "ERROR: 'grok' CLI not on PATH (LOOP_AGENT=grok)"; exit 1; } ;;
esac
command -v python3 >/dev/null 2>&1 || { echo "ERROR: 'python3' not on PATH"; exit 1; }
[ -f "$ROOT/scripts/next_todo.py" ]  || { echo "ERROR: scripts/next_todo.py missing — wrong repo?";  exit 1; }
[ -f "$ROOT/scripts/loop_stats.py" ] || { echo "ERROR: scripts/loop_stats.py missing — wrong repo?"; exit 1; }
# Claude settings (allowlist + Stop hook) only required on the Claude path.
if [ "$LOOP_AGENT" = "claude" ]; then
  [ -f "$SETTINGS" ] || { echo "ERROR: allowlist missing: $SETTINGS"; exit 1; }
fi
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "ERROR: not a git repo: $ROOT"; exit 1; }
# Numeric env guards — a non-numeric value would otherwise spam `[: integer expression expected`
# (MAX_ITERS) or make `timeout` error every pass (PASS_TIMEOUT). Fail fast with a clear message.
case "$MAX_ITERS"    in ''|*[!0-9]*) echo "ERROR: LOOP_MAX_ITERS must be a non-negative integer (got '$MAX_ITERS')"; exit 1 ;; esac
case "$PASS_TIMEOUT" in ''|*[!0-9]*) echo "ERROR: LOOP_PASS_TIMEOUT_SECS must be a non-negative integer (got '$PASS_TIMEOUT')"; exit 1 ;; esac
case "$ERROR_RETRIES"      in ''|*[!0-9]*) echo "ERROR: LOOP_ERROR_RETRIES must be a non-negative integer (got '$ERROR_RETRIES')"; exit 1 ;; esac
# Decimal-accepting (unlike the integer patterns above): budgets are dollar amounts like 37.50.
if [ -n "$RUN_BUDGET" ]; then
  case "$RUN_BUDGET" in *[!0-9.]*|*.*.*|.) echo "ERROR: LOOP_MAX_RUN_BUDGET_USD must be a non-negative number (got '$RUN_BUDGET')"; exit 1 ;; esac
fi
case "$ERROR_BACKOFF_SECS" in ''|*[!0-9]*) echo "ERROR: LOOP_ERROR_RETRY_BACKOFF_SECS must be a non-negative integer (got '$ERROR_BACKOFF_SECS')"; exit 1 ;; esac
LOOP_MAX_TURNS="${LOOP_MAX_TURNS:-}"
if [ -n "$LOOP_MAX_TURNS" ]; then
  case "$LOOP_MAX_TURNS" in *[!0-9]*|0) echo "ERROR: LOOP_MAX_TURNS must be a positive integer (got '$LOOP_MAX_TURNS')"; exit 1 ;; esac
fi
# Model — agent-specific defaults + validation. Resolved here (not down by agent_args assembly) so
# an invalid value fails fast before any sync/keep-awake/heartbeat setup.
LOOP_EFFORT="${LOOP_EFFORT:-max}"
case "$LOOP_AGENT" in
  claude)
    # Closed two-value choice (opus|sonnet): each resolves to the bare CLI alias Claude Code keeps
    # pointed at that family's most advanced released model. Empty → default opus (same as :-).
    LOOP_MODEL="$(printf '%s' "${LOOP_MODEL:-opus}" | tr '[:upper:]' '[:lower:]')"
    case "$LOOP_MODEL" in
      opus|sonnet) ;;
      *) echo "ERROR: LOOP_MODEL must be 'opus' or 'sonnet' when LOOP_AGENT=claude (got '$LOOP_MODEL')"; exit 1 ;;
    esac
    # Headless async-workflow guard (Claude only). The async `Workflow` tool returns IMMEDIATELY
    # with a task id and re-invokes the agent on a completion callback that a one-shot `claude -p`
    # pass NEVER receives. An agent that yields to a workflow therefore STRANDS its work. Disabling
    # the Workflow tool forces SYNCHRONOUS orchestration (Task/Agent subagents + /fr), which blocks
    # in-turn and commits before the pass ends. Default-ON; re-enable (NOT recommended) with an
    # explicitly EMPTY var: `CLAUDE_CODE_DISABLE_WORKFLOWS= scripts/loop_next_todo.sh`. Uses `-`
    # (NOT `:-`): only UNSET defaults to "1"; deliberately-empty stays empty.
    export CLAUDE_CODE_DISABLE_WORKFLOWS="${CLAUDE_CODE_DISABLE_WORKFLOWS-1}"
    ;;
  grok)
    # Any non-empty model id the grok CLI accepts (default: grok-4.5 — current default model).
    LOOP_MODEL="${LOOP_MODEL:-grok-4.5}"
    [ -n "$LOOP_MODEL" ] || { echo "ERROR: LOOP_MODEL must be non-empty when LOOP_AGENT=grok"; exit 1; }
    # No Claude Workflow tool on this path — leave CLAUDE_CODE_DISABLE_WORKFLOWS alone.
    ;;
esac
# A stats dir INSIDE the repo that is NOT git-ignored would make every per-pass JSONL write dirty the
# tree; sync_main then skips main-sync forever, freezing local TODOS.md → a false "no progress" spin-stop
# on a perfectly healthy backlog. Refuse to start rather than fail mysteriously mid-run.
case "$STATS_DIR" in
  "$ROOT" | "$ROOT"/*)
    # Probe a representative FILE we'd write (not the bare dir) — a dir-only gitignore pattern
    # like ".loop-runs/" matches files UNDER it but not the slash-less directory path itself.
    if ! git check-ignore -q "$STATS_DIR/run.jsonl" 2>/dev/null; then
      echo "ERROR: LOOP_STATS_DIR=$STATS_DIR is inside the repo and not git-ignored."
      echo "       Its per-pass stats JSONL would dirty the tree and permanently skip main-sync"
      echo "       (a false 'no progress' spin-stop). Add it to .gitignore, or point it outside the repo."
      exit 1
    fi
    ;;
esac

# --- single-instance lock (the working tree has exactly ONE writer) ---
# Two drivers in one checkout share one working tree: the dirty tree makes both skip main-sync and
# read the same local TODOS.md, so both pick the SAME item, their agents clobber each other's
# in-flight edits, and the loser's abandoned-mid-land stop then blames the survivor's LIVE files on
# its own pass — advising a DISCARD that would destroy a sibling's near-landed work (observed
# 2026-07-10: a duplicate driver started at 16:27 while the 11:32 leader was 12 min into L4820; the
# duplicate's agent spent 12 min/$3.07 proving it must do nothing, then the duplicate driver told
# the operator to land-or-discard the leader's in-flight files, which landed fine 20 min later).
# The lock lives in this checkout's git dir (per-working-tree — sibling worktrees never share a
# tree, so they may run their own loops), holds the driver PID, and self-heals: a holder that is
# dead or not a loop driver (PID recycling) is stale and taken over. The LOOP_DRY_RUN peek honors
# it too — "a loop is already draining this checkout" IS the answer the peek was after.
# LOOP_FORCE=1 bypasses lock + orphan probe (emergencies only — concurrent writers are on you).
LOCK_FILE="$(git rev-parse --absolute-git-dir)/loop_next_todo.lock"
ROOT_PHYS="$(cd "$ROOT" && pwd -P)"
LOOP_LOCK_HELD=0
release_loop_lock() {
  [ "$LOOP_LOCK_HELD" = "1" ] || return 0
  # Remove only OUR lock — never a later run's takeover of a lock this run leaked.
  [ "$(head -n 1 "$LOCK_FILE" 2>/dev/null | tr -cd '0-9')" = "$$" ] && rm -f "$LOCK_FILE" 2>/dev/null
  LOOP_LOCK_HELD=0
  return 0
}
# A live headless /next-todo agent whose cwd is THIS checkout, with no driver: a targeted `kill` of
# the driver releases the lock via its EXIT trap but leaves the synchronous agent child alive and
# still writing the tree (exactly how 2026-07-10's leader landed 28547377 forty minutes after its
# driver died). pgrep matches the agent argv (`… -p /next-todo …` — claude and grok alike; the
# driver's own argv doesn't match); lsof resolves each candidate's real cwd. Best-effort and
# fail-open — no pgrep/lsof or an unreadable cwd just skips the probe; the PID lock stays the
# primary gate.
loop_orphan_agent_pid() {
  local pid cwd
  for pid in $(pgrep -f -- '-p /next-todo' 2>/dev/null || true); do
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    if [ -n "$cwd" ] && [ "$cwd" = "$ROOT_PHYS" ]; then printf '%s\n' "$pid"; return 0; fi
  done
  return 1
}
acquire_loop_lock() {
  local holder holder_cmd orphan
  if [ "${LOOP_FORCE:-0}" = "1" ]; then
    echo "  ⚠ LOOP_FORCE=1 — single-instance lock + orphan-agent probe SKIPPED (concurrent writers on you)"
    return 0
  fi
  # Lock FIRST: a live sibling driver gets the precise "another driver" refusal (its in-flight
  # agent would also trip the orphan probe below, with vaguer advice). Orphan probe runs only
  # once the lock is ours, so it fires exactly on the driverless-straggler case.
  if ( set -C; printf '%s\n' "$$" > "$LOCK_FILE" ) 2>/dev/null; then
    LOOP_LOCK_HELD=1
  else
    holder="$(head -n 1 "$LOCK_FILE" 2>/dev/null | tr -cd '0-9')"
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      holder_cmd="$(ps -p "$holder" -o command= 2>/dev/null || true)"
      case "$holder_cmd" in
        *loop_next_todo*)
          echo "ERROR: another loop_next_todo.sh driver (PID $holder) is already draining this checkout."
          echo "       Two drivers share ONE working tree and clobber each other mid-pass."
          echo "       Wait for it or stop it first:  kill $holder     (lock: $LOCK_FILE)"
          exit 1 ;;
      esac
    fi
    echo "  ⚠ stale lock (holder ${holder:-unknown} is not a live loop driver) — taking over: $LOCK_FILE"
    printf '%s\n' "$$" > "$LOCK_FILE"
    LOOP_LOCK_HELD=1
  fi
  # Release on any exit from here on. The run-level `trap cleanup EXIT` installed later REPLACES
  # this trap; cleanup() calls release_loop_lock itself (idempotent, PID-checked). INT/TERM exit
  # through the EXIT trap; a Ctrl-C in the instant before this trap lands can leak the file —
  # harmless, a dead holder reads stale and is taken over on the next start.
  trap release_loop_lock EXIT
  trap 'exit 130' INT TERM
  if orphan="$(loop_orphan_agent_pid)"; then
    echo "ERROR: a headless /next-todo agent (PID $orphan) is still running in this checkout with no"
    echo "       live driver — an orphaned pass is still writing this tree. Wait for it to finish"
    echo "       or kill it, then re-run. (LOOP_FORCE=1 overrides.)"
    exit 1
  fi
}
acquire_loop_lock

# Selector state as one TAB-separated line: <closed>\t<ready_count>\t<pick>\t<priority>\t<line>\t<title>
# (pick is "P# Title (TODOS.md:LINE)", empty when nothing is ready). The deterministic selector is
# the source of truth — never eyeball TODOS.md to choose. Always prints exactly 6 fields.
select_state() {
  python3 - <<'PY'
import json, subprocess
out = subprocess.run(
    ["python3", "scripts/next_todo.py", "--json"],
    capture_output=True, text=True,
).stdout
try:
    d = json.loads(out) if out.strip() else {}
except json.JSONDecodeError:
    d = {}            # malformed selector stdout → treat as "nothing ready", never abort the loop
closed = d.get("closed", 0)
ready = len(d.get("ready", []))
n = d.get("next")
if n:
    pick = f'{n["priority"]} {n["title"]} (TODOS.md:{n["line"]})'
    print(f'{closed}\t{ready}\t{pick}\t{n["priority"]}\t{n["line"]}\t{n["title"]}')
else:
    print(f'{closed}\t{ready}\t\t\t\t')
PY
}

# Refresh the selector globals (CLOSED READY PICK PRIO LINE TITLE) from the current local TODOS.md.
read_state() {
  local tsv
  tsv="$(select_state || true)"   # never let a selector hiccup abort the loop under set -e
  IFS=$'\t' read -r CLOSED READY PICK PRIO LINE TITLE <<<"$tsv"
}

# Highest PR number currently known to GitHub (open+merged+closed), or "" if gh is unavailable.
# Used to detect PRs the agent opens DURING a pass — the honest "what actually happened" signal.
pr_max() {
  gh pr list --state all --limit 1 --json number -q '.[0].number' 2>/dev/null || true
}

# JSON array of PRs the loop opened THIS pass: number > $1, authored by the current gh user, on a
# claude/* branch. Scoping to @me + claude/* (not just the number threshold) keeps a concurrent
# human/sibling PR in the same number range from being mis-attributed to this item. "[]" if the
# threshold is non-numeric (gh unavailable) so we never mislabel pre-existing PRs as new.
# BLIND SPOT (by design): the number>since filter cannot see a REUSED PR whose number predates this
# pass — that case is surfaced separately by branch_open_pr_json() below (keyed on branch, not number).
#
# gh-FAILURE vs no-PRs: an empty result must NOT conflate "gh genuinely found no new PRs" with "gh
# could not be reached" (offline / auth blip / rate-limit). The `gh` call's exit status is captured
# separately from the python filter, and a non-zero exit (or an unparseable body) emits the distinct
# sentinel {"gh_error": true} instead of "[]". loop_stats `record` decodes the sentinel into
# `pr_check_failed` and labels the PR column "unknown (gh unavailable)" rather than "—", so a reporting
# gap never reads as a definitive no-PR/no-land. (The land DECISION stays anchored on origin/main's
# SHA — git-only, immune to gh — so a gh blip degrades only the PR LABEL, and the pass outcome only
# when origin/main is ALSO unresolvable: see _pass_outcome.)
# gh is probed FIRST — BEFORE the non-numeric-$since short-circuit — so a SUSTAINED outage is caught
# too: when gh is already down at pr_max() time, $since (=pr_before) is "", and short-circuiting on it
# before calling gh would silently return "[]" and read as no-PRs (the exact conflation this guards
# against). Probing first means gh-down → sentinel regardless of $since; gh-up but no numeric baseline
# → "[]" honestly (gh confirmed reachable).
new_prs_json() {
  local since="$1"
  local raw
  if ! raw="$(gh pr list --state all --limit 30 --author @me --json number,title,state,url,headRefName 2>/dev/null)"; then
    echo '{"gh_error": true}'; return 0
  fi
  # gh is up (the probe succeeded). Without a numeric baseline we can't filter number>since without
  # mislabeling every pre-existing PR as new — return "[]", but honestly (not a reporting gap).
  case "$since" in
    ''|*[!0-9]*) echo "[]"; return 0 ;;
  esac
  printf '%s' "$raw" \
    | python3 -c "import json,sys; s=int('$since'); print(json.dumps([{k: p.get(k) for k in ('number','title','state','url')} for p in (json.load(sys.stdin) or []) if p.get('number',0) > s and (p.get('headRefName') or '').startswith('claude/')]))" \
    2>/dev/null || echo '{"gh_error": true}'
}

# Commits on $1 (a claude/* branch) ahead of origin/main — the "work still stranded on the branch"
# count. Empty for a non-claude branch, or when the branch/origin-main ref is unavailable. origin/main
# is freshly fetched by sync_main just before this runs, so the count reflects the post-merge state
# (0 once the branch's work has landed; >0 means a reused/unmerged branch left work behind).
branch_commits_ahead() {
  local branch="$1"
  case "$branch" in claude/*) ;; *) echo ""; return 0 ;; esac
  git -C "$ROOT" rev-list --count "origin/main..$branch" 2>/dev/null || echo ""
}

# The OPEN PR (if any) whose HEAD branch is exactly $1, as a JSON object or "null". Keyed on the
# BRANCH, not a number threshold, so it catches a REUSED PR whose number predates this pass and is
# therefore invisible to new_prs_json above — closing the land-detection blind spot for reused branches.
branch_open_pr_json() {
  local branch="$1"
  case "$branch" in claude/*) ;; *) echo "null"; return 0 ;; esac
  gh pr list --head "$branch" --state open --author @me --json number,title,state,url --limit 1 2>/dev/null \
    | python3 -c "import json,sys; a=json.load(sys.stdin) or []; print(json.dumps(a[0]) if a else 'null')" \
    2>/dev/null || echo "null"
}

# origin/main commit SHA — the AUTHORITATIVE "did this pass land a commit" anchor. sync_main fetches
# origin/main even when the working tree is dirty (it only declines the local ff-checkout), so a
# pushed commit is visible here regardless of the local checkout state. This sidesteps the brittle
# `closed`-count proxy entirely: that count only ticks when the agent strikes the item IN PLACE *and
# keeps its `**Priority:**` line*; an agent that replaces the line with `**Done:**` drops the item
# from the tally and a real landing reads as "no progress" (the L178/L359 misfire that mislabelled
# two real commits and tripped a FALSE spin-stop). "" when origin/main is unresolvable.
origin_main_sha() { git -C "$ROOT" rev-parse origin/main 2>/dev/null || echo ""; }

fmt_dur_sh() { local s="${1:-0}"; printf '%dm%02ds' "$((s / 60))" "$((s % 60))"; }

# Decide what to do with a SAME-pick spin whose PREVIOUS pass was a transient hard ERROR (rc≠0 /
# is_error — e.g. "API Error: Connection closed mid-response"). Pure + side-effect-free so it is
# unit-testable in isolation (tests/scripts/test_loop_error_retry.py extracts it by regex, exactly
# like new_prs_json). An ERROR pass means the item was NEVER adjudicated — the agent died mid-
# orientation, not on a genuine no-progress decision — so re-attempting it (a fresh `claude -p` = a
# fresh connection) is right, NOT stranding the whole ready backlog behind a one-off network blip.
# Prints exactly one of:
#   "retry <n>"  — attempt the item again; <n> is the 1-based attempt number (for the display line).
#   "stop"       — $limit consecutive ERRORs on this pick: a SUSTAINED outage, give up.
# Args: $1 = ERROR retries already spent on this pick, $2 = the limit (LOOP_ERROR_RETRIES).
# limit=0 ⇒ always "stop" (restores the pre-retry behaviour: a transient error spin-stops at once).
error_retry_decision() {
  local spent="${1:-0}" limit="${2:-0}"
  if [ "$spent" -lt "$limit" ]; then
    printf 'retry %d\n' "$((spent + 1))"
  else
    printf 'stop\n'
  fi
}

# Decide whether a `blocked` (decision-fork) spin should AUTO-PARK the item and CONTINUE the drain,
# or STOP for the operator. Pure + side-effect-free so it is unit-testable in isolation
# (tests/scripts/test_loop_auto_park.py extracts it by regex, exactly like error_retry_decision).
# Prints "park" ONLY when parking is both SAFE and PRODUCTIVE:
#   * the item was NOT already auto-parked this run ($1==0) — a re-offer AFTER we parked it means the
#     park didn't stick or the item is genuinely undecidable-recurring; that is a REAL stall the
#     operator must SEE, never silent churn (the ticket's one-park-per-item cap);
#   * the loop is on `main` ($2=="main") — the docs(todos) commit lands on main, so an off-main tree
#     (another worktree holds main; sync_main's `checkout main` was refused) must NOT be committed to;
#   * the working tree is clean ($3==0) — never fold unrelated edits into the auto-park commit.
# Any one unmet ⇒ "stop" (fall back to the manual decision prompt). Args: $1=already_parked(0/1)
# $2=current_branch $3=dirty(0/1).
auto_park_decision() {
  if [ "${1:-0}" = "0" ] && [ "${2:-}" = "main" ] && [ "${3:-0}" = "0" ]; then
    printf 'park\n'
  else
    printf 'stop\n'
  fi
}

AUTO_PARK_REASON=""   # set by auto_park_and_land (below) for the caller's console log
# Park the decision-fork item whose heading is at TODOS line $4, and LAND a one-line docs(todos)
# edit on main. Returns 0 on a fully-landed park (marker VERIFIED re-bucketed PARKED by next_todo,
# committed, and PUSHED); returns non-zero with the working tree + HEAD fully RESTORED on ANY
# failure — so a failed park never strands an unpushed commit or a dirty edit (the next pass's
# sync_main would otherwise choke on it). Orchestrates already-tested pieces: `next_todo.py park`
# (verify-then-write) + `loop_stats.py record-autopark`. Kept a function so the bash integration
# test can drive it in a temp git repo. Args: $1=root $2=todos-path $3=run-file $4=line $5=title
# $6=priority $7=iter. (python scripts resolve from cwd = repo root, as everywhere else in the loop.)
auto_park_and_land() {
  local root="$1" todos="$2" run_file="$3" line="$4" title="$5" prio="$6" iter="$7" kind="${8:-decision fork}"
  local reason head_before default_reason commit_msg
  # Kind-specific wording: a `decision fork` park awaits an operator DECISION; a `bg-yield` (stall)
  # park awaits the operator running the long op SYNCHRONOUSLY; a `no-land` park is the generic
  # same-pick-no-progress case the classifier could NOT tag stall/fork (ran the item, landed nothing,
  # clean tree — inspect/rescope). The decision-fork commit PREFIX ("auto-park decision-fork item")
  # is unchanged so its regression oracle still pins.
  case "$kind" in
    bg-yield)
      default_reason="agent ended its turn awaiting a background-job callback headless never delivers"
      commit_msg="docs(todos): auto-park stalled item — ${title:0:60} (needs synchronous verification)"
      ;;
    no-land)
      default_reason="agent ran this item and landed nothing (clean tree; no stall/fork signal)"
      commit_msg="docs(todos): auto-park no-land item — ${title:0:60} (ran, landed nothing — inspect/rescope)"
      ;;
    *)
      default_reason="agent escalated a design decision it can't resolve headless"
      commit_msg="docs(todos): auto-park decision-fork item — ${title:0:60} (awaiting design decision)"
      ;;
  esac
  reason="$(python3 scripts/loop_stats.py park-summary --run-file "$run_file" 2>/dev/null || true)"
  [ -n "$reason" ] || reason="$default_reason"
  head_before="$(git -C "$root" rev-parse HEAD 2>/dev/null || echo '')"
  # 1) verify-then-write the trigger-gated marker (non-zero ⇒ nothing written, tree still clean).
  python3 scripts/next_todo.py park --file "$todos" --line "$line" --reason "$reason" || return 1
  # 2) commit the one-line docs edit; on failure restore the working tree. Restore from HEAD, NOT
  #    the bare index — `git add` (above) already staged the park edit, so `checkout -- <path>` would
  #    restore FROM that staged bad edit and leave the tree dirty (M TODOS.md), which then freezes
  #    sync_main indefinitely. `checkout HEAD -- <path>` resets both index and worktree to HEAD.
  if ! { git -C "$root" add TODOS.md \
         && git -C "$root" commit -q -m "$commit_msg"; }; then
    git -C "$root" checkout -q HEAD -- TODOS.md 2>/dev/null || true
    return 2
  fi
  # 3) push to main; on failure UNDO the local commit so local main never diverges from origin.
  if ! git -C "$root" push -q origin HEAD:main 2>/dev/null; then
    [ -n "$head_before" ] && git -C "$root" reset --hard -q "$head_before" >/dev/null 2>&1 || true
    return 3
  fi
  # 4) record the loud decision-queue event (best-effort — the park itself already landed).
  python3 scripts/loop_stats.py record-autopark --run-file "$run_file" --iter "$iter" \
    --priority "$prio" --title "$title" --line "$line" --reason "$reason" --kind "$kind" 2>/dev/null || true
  AUTO_PARK_REASON="$reason"
  return 0
}

# Shared auto-park ATTEMPT for a no-land spin whose item can't drain headless as-is — either a
# `blocked` decision-fork (no AskUserQuestion under `claude -p`) or a `stalled` background-job yield (a
# long op run as run_in_background whose completion callback headless never delivers). BOTH mean "park
# this item trigger-gated and move on", and both are definitionally CLEAN-tree here (loop_stats
# classifies a DIRTY no-land as `abandoned`, not stalled/blocked — see _pass_outcome — and the
# abandoned-mid-land guard already exit-3'd the dirty case last pass), so parking strands no recoverable
# work either way. Returns 0 when the item was parked + landed (caller `continue`s the drain) and
# appends the parked $PICK to AUTOPARKED (the one-park-per-item cap); returns non-zero when the park was
# DECLINED (already parked this run / off-main / dirty) or FAILED (auto_park_and_land already RESTORED
# the tree) — caller falls back to the manual operator stop. Reads the loop globals
# ($PICK/$LINE/$TITLE/$PRIO/$iter/$ROOT/$RUN_FILE/$AUTOPARKED) directly, like the arms it factors.
# Args: $1 = kind ("decision fork" | "bg-yield") — threaded to auto_park_and_land for the commit
# message + the decision-queue label.
attempt_auto_park() {
  local kind="$1" branch_now dirty_now already_parked
  branch_now="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  dirty_now=0; [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ] && dirty_now=1
  already_parked=0
  printf '%s\n' "$AUTOPARKED" | grep -qxF "$PICK" && already_parked=1
  if [ "$(auto_park_decision "$already_parked" "$branch_now" "$dirty_now")" = "park" ] \
     && auto_park_and_land "$ROOT" "$ROOT/TODOS.md" "$RUN_FILE" "$LINE" "$TITLE" "$PRIO" "$iter" "$kind"; then
    AUTOPARKED="$AUTOPARKED
$PICK"
    return 0
  fi
  return 1
}

# --- heartbeat (background liveness ticker; quiet mode only) ---
HEARTBEAT_PID=""
start_heartbeat() {
  HEARTBEAT_PID=""
  [ "${LOOP_VERBOSE:-0}" = "1" ] && return 0          # the stream IS the liveness signal
  case "$HEARTBEAT_SECS" in ''|*[!0-9]*|0) return 0 ;; esac
  local iter="$1" label="$2" start="$3"
  (
    set +e
    while :; do
      sleep "$HEARTBEAT_SECS"
      now=$(date +%s); elapsed=$((now - start))
      br=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
      printf '   · still working [%s] %s — %s elapsed · branch %s\n' \
        "$iter" "$label" "$(fmt_dur_sh "$elapsed")" "$br"
    done
  ) &
  HEARTBEAT_PID=$!
}
stop_heartbeat() {
  [ -n "$HEARTBEAT_PID" ] || return 0
  kill "$HEARTBEAT_PID" 2>/dev/null || true
  wait "$HEARTBEAT_PID" 2>/dev/null || true
  HEARTBEAT_PID=""
}

# --- keep-awake (macOS): hold a power assertion for the loop's whole lifetime ---
# An idle laptop sleeping mid-run suspends the `claude` process and drops the network, killing an
# overnight drain. `caffeinate -i -m -s` prevents idle-system + disk-idle + (on AC) system sleep;
# `-w $$` makes it wait on THIS script's PID and self-exit when the loop ends, so it's scoped to the
# run with no orphan. That self-scoping is the PRIMARY teardown; cleanup() additionally releases it
# (verify-before-kill, so a recycled PID can't hit an unrelated process). Display sleep (-d) is left
# alone — headless overnight needs no screen. See the KEEP-AWAKE header note for the AC/lid caveat.
# Engaged once at startup; not re-asserted mid-run (caffeinate does not spontaneously exit, and the
# startup liveness check below surfaces the one realistic failure — a fork that never took).
CAFFEINATE_FLAGS=(-i -m -s)   # array, not a scalar: no word-split/glob surprise if flags ever change
CAFFEINATE_PID=""

# Decide what the keep-awake step will do and echo ONE stable "keep-awake: <plan>" line. The dry-run
# preview, the real-run startup log, and the test gate all key on these three leading substrings:
#   "keep-awake: caffeinate ..."  → engage   |  "keep-awake: disabled ..."  → opted out
#   "keep-awake: skipped ..."     → no-op (non-macOS, or caffeinate missing) — loop runs unchanged
keepawake_plan() {
  if [ "$NO_CAFFEINATE" = "1" ]; then
    echo "keep-awake: disabled (LOOP_NO_CAFFEINATE=1) — machine may sleep normally"
  elif [ "$KEEPALIVE_OS" != "Darwin" ]; then
    echo "keep-awake: skipped (non-macOS: ${KEEPALIVE_OS:-unknown}) — no caffeinate"
  elif ! command -v caffeinate >/dev/null 2>&1; then
    echo "keep-awake: skipped (caffeinate not found on macOS) — install/path it to enable"
  else
    echo "keep-awake: caffeinate ${CAFFEINATE_FLAGS[*]} -w $$ (scoped to loop) — assumes AC power, lid open"
  fi
}

# Engage keep-awake for a REAL run iff the plan says to. Genuinely safe to call unconditionally:
# no-op (returns 0) in dry-run, for disabled/skipped, or if already engaged.
start_keepawake() {
  [ "${LOOP_DRY_RUN:-0}" = "1" ] && return 0   # dry-run only PREVIEWS the plan; never starts a process
  [ -n "$CAFFEINATE_PID" ] && return 0
  case "$(keepawake_plan)" in
    "keep-awake: caffeinate"*) ;;          # engage
    *) return 0 ;;                          # disabled / skipped → nothing to start
  esac
  caffeinate "${CAFFEINATE_FLAGS[@]}" -w $$ &
  CAFFEINATE_PID=$!
  # `command -v` passed in keepawake_plan, but the fork itself can still fail to take — `$!` would
  # then be a dead PID and the run would believe it's protected while the laptop sleeps. Confirm the
  # process is alive; if not, say so honestly and clear the PID. The loop runs either way.
  if ! kill -0 "$CAFFEINATE_PID" 2>/dev/null; then
    echo "  ⚠ keep-awake: caffeinate did not start — the machine may sleep mid-run (loop continues)."
    CAFFEINATE_PID=""
  fi
}
# Release the assertion. Verify the PID is still OUR caffeinate before signalling: if caffeinate had
# died early and macOS recycled its PID, a blind kill could terminate an unrelated process. `-w $$`
# already self-terminates it at script exit, so this is a prompt-release backstop, not the mechanism.
stop_keepawake() {
  [ -n "$CAFFEINATE_PID" ] || return 0
  case "$(ps -p "$CAFFEINATE_PID" -o comm= 2>/dev/null)" in
    *caffeinate) kill "$CAFFEINATE_PID" 2>/dev/null || true ;;
  esac
  CAFFEINATE_PID=""
}

# Pull the just-merged strike into the local working copy so the NEXT selection sees it.
# /next-todo merges on the remote; without this the local TODOS.md never updates and the loop
# would re-pick the item it just finished (a false "no progress"). Safe no-op when dirty/offline.
# It prints a loud warning whenever it declines to sync (dirty tree, or `checkout main` refused) so a
# stale local pick is never silent — the agent does its own sync, so it can still pick differently.
sync_main() {
  git fetch --quiet origin main 2>/dev/null || return 0
  if [ -n "$(git status --porcelain)" ]; then
    echo "  ⚠ working tree dirty — skipping main sync; the printed pick reads LOCAL TODOS.md and"
    echo "    may lag what already merged (the agent does its own sync, so it can pick differently)."
    return 0
  fi
  if ! git checkout --quiet main 2>/dev/null; then
    echo "  ⚠ could not check out main (is it checked out in another worktree? run the loop from the"
    echo "    main checkout) — local TODOS.md will NOT ff-sync, so the printed pick may be stale."
    return 0
  fi
  git merge --ff-only --quiet origin/main 2>/dev/null || true
}

# --- assemble the agent flags shared by every pass (claude | grok) ---
# LOOP_MODEL/LOOP_EFFORT/LOOP_AGENT were already resolved + validated in the preconditions block.
# Both agents: fresh headless session per pass (`-p "/next-todo"`) = the /clear between items.
# Effort is carried solely by the explicit `--effort` flag on both CLIs.
AGENT_BIN=""
agent_args=()
case "$LOOP_AGENT" in
  claude)
    # Max synchronous drain: max effort + async workflows DISABLED (see CLAUDE_CODE_DISABLE_WORKFLOWS
    # guard) so thoroughness comes from a powerful model under SYNCHRONOUS Task/Agent + /fr that
    # commits in-turn. SETTINGS carries the curated allowlist + loop Stop-guard hook.
    AGENT_BIN="claude"
    agent_args=( -p "/next-todo" --model "$LOOP_MODEL" --effort "$LOOP_EFFORT" )
    if [ "${LOOP_VERBOSE:-0}" = "1" ]; then
      agent_args+=( --output-format stream-json --verbose )   # stream-json requires --verbose
    else
      agent_args+=( --output-format json )
    fi
    agent_args+=( --settings "$SETTINGS" )
    if [ "${LOOP_BYPASS_PERMISSIONS:-0}" = "1" ]; then
      agent_args+=( --dangerously-skip-permissions )
    else
      agent_args+=( --permission-mode auto )   # "safe yellow" allowlist unattended
    fi
    if [ -n "$MAX_BUDGET" ]; then agent_args+=( --max-budget-usd "$MAX_BUDGET" ); fi
    ;;
  grok)
    # Grok Build headless: same prompt, different flags. No --settings / no --max-budget-usd.
    # Unattended always --yolo (Grok can't load this loop's Claude settings allowlist; without
    # auto-approve headless hangs on tool prompts). Optional LOOP_MAX_TURNS is the spend-less
    # runaway guard. Stop hooks are PASSIVE on Grok — dirty-tree + auto-park remain the backstops.
    AGENT_BIN="grok"
    agent_args=( -p "/next-todo" -m "$LOOP_MODEL" --effort "$LOOP_EFFORT" --yolo )
    if [ "${LOOP_VERBOSE:-0}" = "1" ]; then
      agent_args+=( --output-format streaming-json )
    else
      agent_args+=( --output-format json )
    fi
    if [ -n "$LOOP_MAX_TURNS" ]; then agent_args+=( --max-turns "$LOOP_MAX_TURNS" ); fi
    ;;
esac
# Back-compat alias used by a few comments/tests that still say claude_args — same array.
claude_args=( "${agent_args[@]}" )

# Per-pass watchdog. Default 4h — a generous wedge-only backstop sized deliberately ABOVE the
# budget-cap wall-clock envelope, so it never truncates a legitimate long vertical (the budget cap
# fires first on any spend-active pass); it exists to reap a hung zero-spend process. 0 disables.
TIMEOUT_BIN=""
if [ "$PASS_TIMEOUT" != "0" ]; then
  if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout"
  else echo "  ⚠ LOOP_PASS_TIMEOUT_SECS set but no 'timeout' binary — running without a watchdog."; fi
fi

# Resolve the model-family alias to a concrete model id for the banner (Claude only).
# Claude: probe `claude -p` stream-json init event for the resolved id (e.g. opus → claude-opus-4-8).
# Grok: models are already concrete ids — return empty so the banner prints the bare LOOP_MODEL.
# Best-effort + fail-soft: ANY failure returns empty. Banner nicety only, never load-bearing.
resolve_model_version() {
  local family="$1" timeout_bin="" line resolved
  [ "$LOOP_AGENT" = "claude" ] || { printf ''; return 0; }
  if command -v timeout >/dev/null 2>&1; then timeout_bin="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then timeout_bin="gtimeout"
  fi
  if [ -n "$timeout_bin" ]; then
    line="$("$timeout_bin" 15 claude -p "hi" --model "$family" --bare --output-format stream-json --verbose 2>/dev/null | head -n 1)" || true
  else
    line="$(claude -p "hi" --model "$family" --bare --output-format stream-json --verbose 2>/dev/null | head -n 1)" || true
  fi
  resolved="$(printf '%s' "$line" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("model", ""))
except Exception:
    pass
' 2>/dev/null)" || true
  printf '%s' "$resolved"
}

# --- run-config banner: state the resolved model / effort / important params up front ---
# Every value is read from the SAME variables the agent flags are built from, so the banner can
# NEVER drift from what's actually passed — it reports reality, not intent.
print_run_config() {
  local thoroughness model_display perm budget runbudget iters twatch erlabel hb outfmt max_turns_label

  case "$LOOP_AGENT" in
    claude)
      # Effective async-workflow state (Claude only).
      local wf_disabled=""
      [ -n "${CLAUDE_CODE_DISABLE_WORKFLOWS:-}" ] && wf_disabled=1
      local model_label
      case "$LOOP_MODEL" in
        opus) model_label="Opus" ;;
        sonnet) model_label="Sonnet" ;;
        *) model_label="$LOOP_MODEL" ;;
      esac
      if [ "$LOOP_EFFORT" = "max" ] && [ -n "$wf_disabled" ]; then
        thoroughness="max synchronous ($model_label max effort · Task/Agent + /fr, async workflows off)"
      elif [ "$LOOP_EFFORT" = "max" ]; then
        thoroughness="max + async workflows ON ($model_label)  ⚠ async workflows can strand work in a headless pass"
      else
        thoroughness="custom override — model=$model_label effort=$LOOP_EFFORT, async_workflows=$([ -n "$wf_disabled" ] && echo off || echo on)"
      fi
      local resolved_model
      resolved_model="$(resolve_model_version "$LOOP_MODEL")"
      if [ -n "$resolved_model" ]; then
        model_display="$resolved_model (alias: $LOOP_MODEL)"
      else
        model_display="$LOOP_MODEL (exact version unresolved — claude probe failed or timed out)"
      fi
      if [ "${LOOP_BYPASS_PERMISSIONS:-0}" = "1" ]; then
        perm="bypass (--dangerously-skip-permissions)"
      else
        perm="auto (safe-yellow allowlist)"
      fi
      if [ -n "$MAX_BUDGET" ]; then budget="\$$MAX_BUDGET/pass"; else budget="disabled (no cap)"; fi
      if [ "${LOOP_VERBOSE:-0}" = "1" ]; then outfmt="stream-json (--verbose)"; else outfmt="json"; fi
      ;;
    grok)
      if [ "$LOOP_EFFORT" = "max" ]; then
        thoroughness="max synchronous (Grok $LOOP_MODEL max effort · commit-in-turn; no Claude async Workflow tool)"
      else
        thoroughness="custom override — agent=grok model=$LOOP_MODEL effort=$LOOP_EFFORT"
      fi
      model_display="$LOOP_MODEL"
      perm="yolo (Grok unattended; no Claude settings allowlist path)"
      if [ -n "$MAX_BUDGET" ]; then
        budget="n/a on Grok (LOOP_MAX_BUDGET_USD=\$$MAX_BUDGET ignored — use pass-timeout / LOOP_MAX_TURNS)"
      else
        budget="n/a on Grok (no --max-budget-usd; use pass-timeout / LOOP_MAX_TURNS)"
      fi
      if [ "${LOOP_VERBOSE:-0}" = "1" ]; then outfmt="streaming-json"; else outfmt="json"; fi
      ;;
  esac

  if [ -n "$RUN_BUDGET" ]; then
    runbudget="\$$RUN_BUDGET/run (advisory aggregate — only counts passes that report cost_usd)"
  else
    runbudget="disabled"
  fi
  if [ "$MAX_ITERS" -gt 0 ]; then iters="$MAX_ITERS"; else iters="unlimited (drain until empty)"; fi
  if   [ "$PASS_TIMEOUT" = "0" ];     then twatch="none"
  elif [ -n "$TIMEOUT_BIN" ];         then twatch="${PASS_TIMEOUT}s ($TIMEOUT_BIN)"
  else                                     twatch="${PASS_TIMEOUT}s — NO timeout binary, watchdog disabled"; fi
  if [ "$ERROR_RETRIES" = "0" ]; then erlabel="off (a transient rc≠0 pass spin-stops immediately)"
  else erlabel="up to ${ERROR_RETRIES}× same-item on a transient rc≠0 error (${ERROR_BACKOFF_SECS}s backoff)"; fi
  if [ "${LOOP_VERBOSE:-0}" = "1" ]; then hb="off (verbose stream is the liveness signal)"
  else case "$HEARTBEAT_SECS" in ''|*[!0-9]*|0) hb="off" ;; *) hb="${HEARTBEAT_SECS}s" ;; esac; fi
  if [ -n "$LOOP_MAX_TURNS" ]; then max_turns_label="$LOOP_MAX_TURNS"; else max_turns_label="off"; fi

  echo "Run config:"
  echo "  agent        : $LOOP_AGENT"
  echo "  thoroughness : $thoroughness"
  echo "  model        : $model_display"
  echo "  effort       : $LOOP_EFFORT"
  echo "  permission   : $perm"
  echo "  budget       : $budget"
  echo "  run-budget   : $runbudget"
  echo "  max-turns    : $max_turns_label"
  echo "  max-iters    : $iters"
  echo "  pass-timeout : $twatch"
  echo "  error-retry  : $erlabel"
  echo "  heartbeat    : $hb"
  echo "  output       : $outfmt"
  if [ "$LOOP_AGENT" = "claude" ]; then
    echo "  settings     : $SETTINGS"
  else
    echo "  settings     : n/a (Grok — Stop hook passive; dirty-tree + auto-park are the backstops)"
  fi
  echo "  stats-dir    : $STATS_DIR"
}

if [ "${LOOP_DRY_RUN:-0}" = "1" ]; then
  read_state
  echo "DRY RUN — would drain TODOS.md starting from:"
  echo "  next: ${PICK:-<none ready>}"
  print_run_config
  echo "  cmd : $AGENT_BIN ${agent_args[*]}"
  echo "  $(keepawake_plan)"
  exit 0
fi

# --- run-level state + always-print summary on exit ---
mkdir -p "$STATS_DIR"
RUN_FILE="$STATS_DIR/run-$(date -u +%Y%m%dT%H%M%SZ)-$$.jsonl"
STOP_REASON=""
SUMMARY_PRINTED=0
PASS_LOG=""        # current pass's captured stdout/stderr — tracked so an interrupt mid-pass
PASS_ERR=""        # doesn't leak the temp files (cleanup removes whatever is still set).
print_summary() {
  local rc="${1:-0}"
  [ "$SUMMARY_PRINTED" = "1" ] && return 0
  [ -f "$RUN_FILE" ] || return 0          # nothing recorded yet (e.g. exited before pass 1)
  SUMMARY_PRINTED=1
  local reason="$STOP_REASON"
  if [ -z "$reason" ]; then               # derive an honest reason from the real exit code
    case "$rc" in
      0)   reason="backlog drained — no READY item left" ;;
      130) reason="interrupted (signal)" ;;
      *)   reason="crashed (rc=$rc)" ;;
    esac
  fi
  python3 scripts/loop_stats.py summary \
    --run-file "$RUN_FILE" \
    --reason "$reason" \
    --ready-remaining "${READY:-?}" || true
}
cleanup() {
  local rc=$?                             # capture FIRST — before any command resets $?
  stop_heartbeat
  print_summary "$rc"
  rm -f "$PASS_LOG" "$PASS_ERR" 2>/dev/null || true
  release_loop_lock                       # single-instance lock: this run is over either way
  stop_keepawake                          # release LAST: stay awake through summary + cleanup (-w $$ covers exit)
}
trap cleanup EXIT
trap 'STOP_REASON="interrupted (signal)"; exit 130' INT TERM

echo "Stats → $RUN_FILE"
print_run_config           # state the resolved model/effort/budget/... so the run is never ambiguous
echo "$(keepawake_plan)"   # log the keep-awake decision so an operator sees it up front
start_keepawake            # hold the assertion now; cleanup()/`-w $$` release it when the loop ends
sync_main   # begin from up-to-date main
read_state  # CLOSED READY PICK PRIO LINE TITLE

iter=0
prev_pick=""
prev_progressed=0   # did the PREVIOUS pass advance origin/main? (1=landed a commit, 0=nothing landed)
prev_outcome=""     # the PREVIOUS pass's one-word outcome (stalled|blocked|no-land|ERROR|…) — drives
                    # the spin-stop message so a bg-yield/decision-fork/transient-error stop doesn't
                    # advise a blind re-run
err_retries=0       # consecutive transient-ERROR retries spent on the CURRENT stuck pick; reset to 0
                    # after any non-ERROR pass (below) so a later ERROR on a different item — or after
                    # a recovery — gets its own fresh retry budget rather than inheriting a stale count.
AUTOPARKED=""       # newline-delimited PICKs the decision-fork backstop AUTO-PARKED this run — the
                    # one-park-per-item cap: an item re-offered AFTER we parked it is a REAL stall the
                    # operator must see (park didn't stick / undecidable-recurring), never silent churn.
                    # Keyed on the full $PICK (priority+title+"(TODOS.md:LINE)"), the SAME identity the
                    # spin-guard uses — NOT the bare title, which collides when TODOS reuses a heading.
while [ -n "$PICK" ]; do
  # Spin-guard. The selector re-surfacing the SAME pick is only "stuck" if the last pass ALSO landed
  # nothing. When the agent's own /next-todo applies judgment and works a DIFFERENT item — e.g. the
  # selector's #1 carries a PROSE-only deferral ("…so deferred") the deterministic selector can't see,
  # so it never gets struck and stays #1 — the backlog is still draining and the loop must continue.
  # Stopping purely on "same pick twice" (the old guard) halted a healthy run after the agent landed a
  # real commit on a different item (the L322 false spin-stop). Now: stop ONLY on same-pick AND
  # no-commit-landed; on same-pick BUT a landing, advise the operator to park the pick and keep going.
  if [ "$PICK" = "$prev_pick" ]; then
    if [ "$prev_progressed" = "1" ]; then
      echo "  ↳ note: selector still ranks this pick #1, but the agent worked & LANDED a different item"
      echo "    last pass — it is repeatedly skipping \"$PICK\" (likely a PROSE-only deferral the selector"
      echo "    can't see). Continuing. Park it (add a **Depends on:** line or SHELVED) so it stops being"
      echo "    re-offered: $PICK"
    else
      # The last pass landed nothing AND the selector re-offers the same pick — a genuine spin.
      # But the RIGHT next move depends on WHY it didn't land, and `prev_outcome` (the same
      # single-source classification loop_stats prints on the per-pass line + summary) tells us.
      # A plain "re-run" is correct ONLY for a generic no-land; for a `stalled` (background-job
      # yield) or `blocked` (decision-fork) last pass a re-run reproduces the exact same stop —
      # the operator must run the long op synchronously / decide / park the item. Tailor the stop
      # message so the LAST thing printed isn't the misleading "inspect, then re-run"
      # (project_loop_next_todo_no_land_root_causes, 11th occurrence: a clean-tree
      # `data reextract` bg-yield spin-stopped with the generic message while 175 ready items
      # sat behind the un-drainable pick).
      case "$prev_outcome" in
        stalled)
          # DETERMINISTIC AUTO-PARK BACKSTOP (the bg-yield sibling of the decision-fork arm below). A
          # `stalled` no-land means the pass launched a long op (make test / a backfill / a probe) as a
          # run_in_background job and ended its turn awaiting a completion callback headless `claude -p`
          # NEVER delivers — the job is killed and nothing lands; a plain re-run reproduces it exactly.
          # Rather than STRAND the whole ready backlog behind one un-drainable verification (this trap
          # once terminated a drain with 43 ready items left on a single `make test` bg-yield), PARK the
          # item (trigger-gated → the operator runs the long op SYNCHRONOUSLY later) and CONTINUE the
          # drain. A stalled pass is definitionally CLEAN-tree (a DIRTY bg-yield classifies `abandoned`
          # and already exit-3'd last pass), so nothing recoverable is stranded. Same one-park-per-item
          # cap + manual-stop fallback as the decision-fork arm; it PARKS the item, it never decides HOW
          # to finish it (still yours).
          if attempt_auto_park "bg-yield"; then
            echo "  ⏸ AUTO-PARKED (bg-yield stall) — continuing the drain: $PICK"
            echo "     Parked it (trigger-gated) + committed a one-line docs(todos) edit to main."
            echo "     Reason (from the pass): $AUTO_PARK_REASON"
            echo "     The FOLLOW-UP is still YOURS — run the item's long op SYNCHRONOUSLY (foreground),"
            echo "     then unpark it. The stalled item surfaces in the run summary's queue. Continuing."
            prev_pick="$PICK"; prev_outcome=""
            read_state          # PICK now = next READY (parked item dropped out); "" if drained
            continue            # re-enter the while; new PICK != prev_pick → no spin → run it
          fi
          # auto-park declined (already parked this run / off-main / dirty) OR failed (tree RESTORED by
          # auto_park_and_land). Fall back to the manual stop with the synchronous-finish guidance.
          STOP_REASON="stalled (background-job yield) on: $PICK (no commit landed)"
          echo "✗ Stalled (background-job yield) on: $PICK"
          echo "  Last pass launched a long op (e.g. data reextract/backfill) as a background job and"
          echo "  ended its turn awaiting a completion callback headless 'claude -p' NEVER delivers — so"
          echo "  the job was killed and nothing landed. A plain re-run will stall the SAME way. Either:"
          echo "    • run the item's long op SYNCHRONOUSLY yourself (foreground), then strike + commit, OR"
          echo "    • PARK the item (add 'SHELVED' / 'trigger-gated' to its **Priority:** line) so the loop"
          echo "      drains past it to the other ready items."
          echo "  (auto-park backstop did NOT fire: item already auto-parked this run, loop not on main,"
          echo "   dirty tree, or the park/commit/push failed — see any message above.)"
          exit 3 ;;
        blocked)
          # DETERMINISTIC AUTO-PARK BACKSTOP — the PREVENTION half of the recurring headless
          # decision-fork trap. A `blocked` no-land means the pass shaped a design but hit a go/no-go
          # it can't make headless (no AskUserQuestion under `claude -p`). Rather than STRAND the whole
          # ready backlog behind one fork (an occurrence once stranded 45 ready items and forced a
          # manual re-run), PARK the item (trigger-gated → the operator decides later) and CONTINUE the
          # drain. This MECHANIZES the "build-your-recommendation or park" action next-todo/SKILL.md
          # asks the agent to do by hand, so the overnight drain self-heals regardless of agent
          # compliance. The shared helper handles the safe+productive gate (not already-parked, on
          # main, clean tree) + the park/commit/push. It PARKS the fork; it never DECIDES it (that stays
          # the operator's call, surfaced in the run summary's decision queue).
          if attempt_auto_park "decision fork"; then
            echo "  ⏸ AUTO-PARKED (decision fork) — continuing the drain: $PICK"
            echo "     Parked it (trigger-gated) + committed a one-line docs(todos) edit to main."
            echo "     Reason (from the pass): $AUTO_PARK_REASON"
            echo "     The DECISION is still YOURS — the parked fork surfaces in the run summary's"
            echo "     decision queue. Continuing to the next ready item."
            prev_pick="$PICK"; prev_outcome=""
            read_state          # PICK now = next READY (parked item dropped out); "" if drained
            continue            # re-enter the while; new PICK != prev_pick → no spin → run it
          fi
          # auto-park declined (already parked this run / off-main / dirty) OR failed (park verify /
          # commit / push — auto_park_and_land already RESTORED the tree). Fall back to the manual stop.
          STOP_REASON="blocked (decision fork) on: $PICK (no commit landed)"
          echo "✗ Blocked (decision fork) on: $PICK"
          echo "  Last pass escalated a genuine decision it can't make headless (AskUserQuestion is"
          echo "  unavailable under 'claude -p'). GO DECIDE, then PARK or rescope the item so the loop"
          echo "  drains past it — a plain re-run will spin-stop here again."
          echo "  (auto-park backstop did NOT fire: item already auto-parked this run, loop not on"
          echo "   main, dirty tree, or the park/commit/push failed — see any message above.)"
          exit 3 ;;
        unknown)
          STOP_REASON="indeterminate on: $PICK (gh AND origin/main both unresolvable — could not confirm a landing)"
          echo "✗ Indeterminate on: $PICK"
          echo "  Last pass could not confirm whether anything landed: the PR check (gh) failed AND"
          echo "  origin/main's SHA was unresolvable, so neither land signal was available. This is a"
          echo "  degraded repo state (main never fetched / missing origin/main ref), not mere offline."
          echo "  Run 'gh auth status' + 'git fetch origin main', then inspect whether the work actually"
          echo "  landed before re-running."
          exit 3 ;;
        ERROR)
          # A transient HARD error (rc≠0 / is_error — e.g. "API Error: Connection closed
          # mid-response") is categorically unlike stalled/blocked/no-land: the agent died
          # mid-orientation, so the item was NEVER attempted. Retry the SAME pick up to
          # $ERROR_RETRIES times (a fresh `claude -p` = a fresh connection) before giving up;
          # only $ERROR_RETRIES CONSECUTIVE errors on this pick — a sustained outage — spin-stops.
          # Pre-retry this halted the ENTIRE drain on the first blip (observed 3/3 across the run
          # history), stranding the ready backlog behind a one-off network event.
          decision="$(error_retry_decision "$err_retries" "$ERROR_RETRIES")"
          case "$decision" in
            retry\ *)
              err_retries="${decision#retry }"   # the 1-based attempt number the decision minted
              echo "  ↻ transient error on: $PICK"
              echo "    Last pass hit a hard API/CLI error (rc≠0) — the item was never attempted."
              echo "    Retrying the SAME item (attempt $err_retries of $ERROR_RETRIES; fresh session)."
              [ "$ERROR_BACKOFF_SECS" != "0" ] && sleep "$ERROR_BACKOFF_SECS"
              # Fall through: do NOT exit. Control leaves the case + the enclosing same-pick `if`
              # and drops into the iter/run block below, re-running this exact pick.
              ;;
            *)
              # err_retries retries + the initial pass that first errored = err_retries+1 attempts.
              attempts=$((err_retries + 1))
              STOP_REASON="repeated transient error on: $PICK ($attempts consecutive rc≠0 passes — likely a sustained API/network outage)"
              echo "✗ Repeated transient error on: $PICK"
              echo "  $attempts consecutive passes hit a hard API/CLI error (rc≠0) on this item."
              echo "  That reads as a SUSTAINED outage, not a one-off blip. Check your connection /"
              echo "  'claude' auth, then re-run the drain — the item was never attempted, so nothing"
              echo "  is lost."
              exit 3 ;;
          esac
          ;;
        *)
          # DETERMINISTIC AUTO-PARK BACKSTOP — the GENERIC-no-land sibling of the stalled/blocked arms
          # above, and the fix for the LAST hard-stop that could still terminate a whole drain. A
          # same-pick no-land the classifier could NOT tag `stalled`/`blocked` is USUALLY the same
          # un-drainable-headless behaviour wearing phrasing the substring classifier didn't recognise
          # (project_loop_next_todo_no_land_root_causes, 17th occurrence: a promote-then-present pass
          # ended its turn stating a pick without building — `terminal_signal:null` → generic `no-land`
          # → this arm HARD-STOPPED, stranding 12 ready items). The classifier has been patched 6× and
          # still misses, so STOP coupling the loop's self-healing to it: PARK any clean-tree same-pick
          # no-land and CONTINUE, regardless of whether it was correctly labelled. Safe by the SAME
          # invariant as the other two arms — a no-land here is definitionally CLEAN-tree (a DIRTY
          # no-land classifies `abandoned` and already exit-3'd last pass), so parking strands nothing
          # recoverable. The one-park-per-item cap (inside attempt_auto_park) still HARD-STOPS a pick
          # re-offered AFTER a park — a park that didn't stick is a REAL stall the operator must see,
          # never silent churn. (unknown/ERROR keep their own arms; only this generic default changes.)
          if attempt_auto_park "no-land"; then
            echo "  ⏸ AUTO-PARKED (no-land) — continuing the drain: $PICK"
            echo "     Parked it (trigger-gated) + committed a one-line docs(todos) edit to main."
            echo "     Reason (from the pass): $AUTO_PARK_REASON"
            echo "     The FOLLOW-UP is still YOURS — the pass ran this item and landed nothing (clean"
            echo "     tree, no stall/fork signal). Inspect or rescope it; it surfaces in the run"
            echo "     summary's queue. Continuing to the next ready item."
            prev_pick="$PICK"; prev_outcome=""
            read_state          # PICK now = next READY (parked item dropped out); "" if drained
            continue            # re-enter the while; new PICK != prev_pick → no spin → run it
          fi
          # auto-park declined (already parked this run / off-main / dirty) OR failed (tree RESTORED by
          # auto_park_and_land). Fall back to the manual stop.
          STOP_REASON="no progress on: $PICK (no commit landed on origin/main last pass)"
          echo "✗ No progress on: $PICK"
          echo "  (no commit landed on origin/main last pass — stopping to avoid a spin loop; inspect, then re-run)"
          echo "  (auto-park backstop did NOT fire: item already auto-parked this run, loop not on main,"
          echo "   dirty tree, or the park/commit/push failed — see any message above.)"
          exit 3 ;;
      esac
    fi
  fi

  iter=$((iter + 1))
  if [ "$MAX_ITERS" -gt 0 ] && [ "$iter" -gt "$MAX_ITERS" ]; then
    STOP_REASON="reached LOOP_MAX_ITERS=$MAX_ITERS"
    echo "Reached LOOP_MAX_ITERS=$MAX_ITERS — stopping."
    break
  fi

  # Snapshot THIS pass before running it (state globals get overwritten by the post-sync refresh).
  p_pick="$PICK"; p_prio="$PRIO"; p_line="$LINE"; p_title="$TITLE"
  closed_before="$CLOSED"
  label="${p_title:0:48}"

  echo "──────────────────────────────────────────────────────────────"
  echo "[$iter] $p_pick"
  echo "──────────────────────────────────────────────────────────────"

  PASS_LOG="$(mktemp)"; PASS_ERR="$(mktemp)"   # run-level globals so cleanup() can rm on interrupt
  pr_before="$(pr_max)"
  om_before="$(origin_main_sha)"   # origin/main BEFORE the pass (the prior sync_main fetched it)
  # Porcelain lines already dirty BEFORE the pass launches. The abandoned-mid-land check below
  # set-diffs against this so it only ever attributes NEW dirt to the pass — pre-existing dirt
  # (operator edits made mid-run, a prior abandonment) is someone else's work, and "LAND or
  # DISCARD" advice pointed at it invites destroying files no pass of ours produced (2026-07-10).
  pre_porcelain="$(git -C "$ROOT" status --porcelain 2>/dev/null || true)"
  start_epoch="$(date +%s)"

  # Build the actual command (optionally wrapped in the watchdog).
  pass_cmd=( "$AGENT_BIN" "${agent_args[@]}" )
  [ -n "$TIMEOUT_BIN" ] && pass_cmd=( "$TIMEOUT_BIN" "$PASS_TIMEOUT" "${pass_cmd[@]}" )

  # A fresh headless session = the "/clear" between every /next-todo.
  if [ "${LOOP_VERBOSE:-0}" = "1" ]; then
    set +e
    "${pass_cmd[@]}" 2>"$PASS_ERR" | tee "$PASS_LOG"
    rc=${PIPESTATUS[0]}
    set -e
  else
    start_heartbeat "$iter" "$label" "$start_epoch"
    set +e
    "${pass_cmd[@]}" >"$PASS_LOG" 2>"$PASS_ERR"
    rc=$?
    set -e
    stop_heartbeat
  fi
  end_epoch="$(date +%s)"

  if [ "$rc" -ne 0 ]; then
    echo "  ⚠ $AGENT_BIN exited rc=$rc — last stderr lines:"
    tail -n 8 "$PASS_ERR" 2>/dev/null | sed 's/^/      /' || true
  fi

  branch_end="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
  # ABANDONED MID-LAND detection — the UNcommitted blind spot, sibling of the committed-but-unmerged
  # branch_commits_ahead/branch_open_pr probes below. Each pass is a fresh `claude -p` with NO
  # continuation (line ~431), so NEW dirt at pass end means the agent ended its turn before
  # committing, classically because it launched a `run_in_background` job and yielded for a
  # completion callback headless `claude -p` never delivers, stranding real work
  # (project_loop_next_todo_no_land_root_causes — 3rd occurrence). .loop-runs/ + LOOP_STATS_DIR are
  # gitignored, so the loop's own writes never register; only genuine pass output trips this.
  # ATTRIBUTION: only porcelain lines ABSENT at pass start count as the pass's abandoned work —
  # dirt that pre-dates the pass belongs to another writer (2026-07-10: a sibling driver's live
  # in-flight files were blamed on a ZERO-edit pass, with LAND-or-DISCARD advice pointed at work
  # that landed fine 20 minutes later). Full-line match, so a pre-existing dirty file the pass
  # staged or edited further changes its status column and correctly resurfaces as pass work.
  # dirty_count (recorded below; loop_stats classifies >0 as `abandoned`) is therefore the
  # PASS-ATTRIBUTABLE count; pre-existing dirt is reported separately and never stops the drain.
  # PREVENTION (7th occurrence): scripts/loop_stop_guard.sh — a Stop hook wired in
  # loop_next_todo.settings.json — now BLOCKS the agent from ending its turn while the tree is
  # dirty, forcing it to finish synchronously or commit IN-turn. This block below is the post-hoc
  # DETECTION backstop for whatever the hook lets through (the hook fires once per session, then
  # allows the stop so it can't infinite-loop, and the clean-tree mid-investigation yield it can't see).
  dirty_porcelain="$(git -C "$ROOT" status --porcelain 2>/dev/null || true)"
  newly_dirty=""
  if [ -n "$dirty_porcelain" ]; then
    if [ -n "$pre_porcelain" ]; then
      newly_dirty="$(printf '%s\n' "$dirty_porcelain" | grep -Fxv -f <(printf '%s\n' "$pre_porcelain") || true)"
    else
      newly_dirty="$dirty_porcelain"
    fi
  fi
  dirty_count=0
  [ -n "$newly_dirty" ] && dirty_count="$(printf '%s\n' "$newly_dirty" | grep -c '^')"
  total_dirty_now=0
  [ -n "$dirty_porcelain" ] && total_dirty_now="$(printf '%s\n' "$dirty_porcelain" | grep -c '^')"
  preexisting_dirty=$((total_dirty_now - dirty_count))
  prev_pick="$p_pick"
  sync_main          # pull the strike/merge so the next selection + closed-count are current
  read_state         # refresh CLOSED READY PICK PRIO LINE TITLE for the next iteration
  # Authoritative progress signal: did the pass push a commit to origin/main? sync_main just
  # re-fetched origin/main, so this is current even if the local tree was too dirty to ff-checkout.
  # Drives BOTH the spin-guard (above, next iteration) and `landed` in the stats — replacing the
  # brittle closed-count delta that mislabelled real landings whose strike dropped the `**Priority:**`
  # line.
  om_after="$(origin_main_sha)"
  # Tri-state head_advanced for the RECORD: "1" origin/main advanced (a landing) · "0" resolved but
  # unchanged (a DEFINITE no-land — a merge would have moved the SHA) · "" EITHER ref unresolvable, so
  # git could not confirm the land state (INDETERMINATE, distinct from a definite no-move). The empty
  # case lets loop_stats report "unknown" instead of a false "no-land" when the gh PR check ALSO failed.
  # NOTE this is RARE, not "offline": `git rev-parse origin/main` reads a LOCAL cached ref and resolves
  # fine offline once main has been fetched, so "" needs a genuinely-unresolvable ref — a fresh clone
  # whose main was never fetched, a missing origin/main, or a broken repo. prev_progressed treats ONLY
  # "1" as progress, so both "" and "0" read as no-progress for the spin-guard — conservative: an
  # unconfirmable landing must not be assumed.
  if [ -z "$om_before" ] || [ -z "$om_after" ]; then
    head_advanced=""
  elif [ "$om_after" != "$om_before" ]; then
    head_advanced=1
  else
    head_advanced=0
  fi
  prev_progressed="$head_advanced"
  new_prs="$(new_prs_json "$pr_before")"
  # Reused-branch land-detection: capture work the pass may have left on branch_end (run AFTER
  # sync_main so origin/main is current). A claude/* branch ahead of main, or with an OPEN PR whose
  # number predates the pass, means the pass did real work that new_prs_json's number filter misses.
  branch_ahead="$(branch_commits_ahead "$branch_end")"
  branch_open_pr="$(branch_open_pr_json "$branch_end")"

  # Gate-trailer verification — the "GOAL landed, not just a commit" check. When the pass landed
  # (origin/main advanced), re-run every `Gate:` commit trailer on the landed range: the trailer
  # persists the item's /goalify check onto the landing commit (cheap deterministic command,
  # repo-root, <~2 min, no live server/DB-state dependency). No trailer → gate_pass stays ""
  # (recorded gate:none — trailers are opt-in per commit; docs-only landings are exempt). Any red
  # → gate_pass=0 and, after the record below, a LOUD exit 3: the item is already struck and
  # pushed, so auto-park is wrong — the operator inspects (or reverts) the landed range.
  gate_pass="" gate_cmd=""
  if [ "$head_advanced" = "1" ] && [ -n "$om_before" ]; then
    gate_trailers="$(git log --format='%(trailers:key=Gate,valueonly)' "$om_before..$om_after" 2>/dev/null | sed '/^[[:space:]]*$/d')" || gate_trailers=""
    if [ -n "$gate_trailers" ]; then
      gate_pass=1
      while IFS= read -r g; do
        [ -n "$g" ] || continue
        gate_cmd="$g"
        echo "  ⛩ gate: $g"
        if [ -n "$TIMEOUT_BIN" ]; then
          "$TIMEOUT_BIN" 300 bash -c "$g" >/dev/null 2>&1 || { gate_pass=0; break; }
        else
          bash -c "$g" >/dev/null 2>&1 || { gate_pass=0; break; }
        fi
      done <<<"$gate_trailers"
    fi
  fi

  # Record + print the per-pass stats line (the "ongoing read").
  # --agent selects the result-schema normalizer (claude | grok); both land in the same JSONL shape.
  python3 scripts/loop_stats.py record \
    --run-file "$RUN_FILE" \
    --iter "$iter" \
    --priority "$p_prio" --title "$p_title" --line "$p_line" \
    --rc "$rc" \
    --started "$start_epoch" --ended "$end_epoch" \
    --closed-before "$closed_before" --closed-after "$CLOSED" \
    --result-file "$PASS_LOG" \
    --agent "$LOOP_AGENT" \
    --new-prs-json "$new_prs" \
    --branch-end "$branch_end" \
    --branch-commits-ahead "$branch_ahead" \
    --branch-open-pr-json "$branch_open_pr" \
    --dirty-count "$dirty_count" \
    --head-advanced "$head_advanced" \
    --gate-pass "$gate_pass" --gate-cmd "$gate_cmd" || true

  # Classify THIS pass (same single-source outcome loop_stats printed above) so the next
  # iteration's spin-guard can tailor its STOP message — stalled/blocked must not advise a blind
  # re-run. Read after the record is written; fail-soft to "" (→ generic stop message).
  prev_outcome="$(python3 scripts/loop_stats.py last-outcome --run-file "$RUN_FILE" 2>/dev/null || echo "")"
  # Reset the transient-ERROR retry budget on any NON-error pass, so a later ERROR on a different
  # item — or a recovery on this one — starts fresh instead of inheriting a stale count. A run of
  # consecutive ERRORs on the same pick keeps the count climbing until the spin-guard's ERROR arm
  # (above, next iteration) exhausts it and spin-stops.
  [ "$prev_outcome" = "ERROR" ] || err_retries=0

  rm -f "$PASS_LOG" "$PASS_ERR"; PASS_LOG=""; PASS_ERR=""

  # Stop LOUDLY + ACCURATELY on an abandoned mid-land. Surfacing the recoverable work here (with the
  # file list) beats looping on: the next pass's sync_main skips-on-dirty, so read_state re-picks the
  # SAME un-attempted item and the top-of-loop guard then halts with the MISLEADING "item did not
  # land" — hiding that real, tested work is sitting uncommitted one `git status` away. Lists ONLY
  # the pass's own new dirt; pre-existing dirty files are named as such, never as landable/discardable.
  if [ "$dirty_count" -gt 0 ]; then
    STOP_REASON="abandoned mid-land — pass [$iter] left $dirty_count uncommitted file(s) in the working tree"
    echo "  ⚠ ABANDONED MID-LAND — pass [$iter] left $dirty_count uncommitted file(s) (RECOVERABLE):"
    printf '%s\n' "$newly_dirty" | head -n 12 | sed 's/^/        /'
    [ "$dirty_count" -gt 12 ] && echo "        … and $((dirty_count - 12)) more"
    [ "$preexisting_dirty" -gt 0 ] && echo "     (+ $preexisting_dirty pre-existing dirty file(s) NOT from this pass — inspect before touching)"
    echo "     The agent ended its turn before committing (headless background-job-callback yield)."
    echo "     Inspect: git -C \"$ROOT\" status   —   then LAND or DISCARD the work before re-running."
    exit 3
  fi
  # Pre-existing dirt alone is NOT an abandonment — it pre-dates the pass, so it is not this
  # pass's to land or discard. Say so and keep draining: the agent syncs main itself in-pass, so
  # landings continue; only the DRIVER's printed pick may lag (sync_main skips while dirty).
  if [ "$preexisting_dirty" -gt 0 ]; then
    echo "  ⚠ note: $preexisting_dirty dirty file(s) pre-date pass [$iter] (untouched by it) — not abandoned"
    echo "    work, and NOT candidates for land/discard here. Commit or clean them when convenient;"
    echo "    until then the driver's printed pick reads a stale local TODOS.md (main-sync skipped)."
  fi

  # Stop LOUDLY on a red gate — a landing whose own persisted check fails is a WRONG FIX pushed to
  # main, worse than a no-land. NO auto-park: the item is already struck and the state points at
  # the next pick; the operator inspects (or reverts) the landed range before the drain continues.
  if [ "$gate_pass" = "0" ]; then
    STOP_REASON="gate-fail — pass [$iter] landed $om_before..$om_after but its Gate: check fails"
    echo "  ✗ GATE RED after a pushed landing:"
    echo "      gate  : $gate_cmd"
    echo "      range : $om_before..$om_after"
    echo "     The commit is already on origin/main — inspect (or revert) the range before re-running the drain."
    exit 3
  fi

  # Advisory RUN-level budget cap. The float comparison lives in loop_stats.py budget-check
  # (macOS bash 3.2 can't compare decimals): exit 3 = over, stdout = the recorded total. Only
  # rc=3 stops the run — any other failure (missing python, corrupt run-file) fail-softs and
  # drains on, because stats are never load-bearing; the per-pass --max-budget-usd is the hard bound.
  if [ -n "$RUN_BUDGET" ]; then
    bc_rc=0
    run_total="$(python3 scripts/loop_stats.py budget-check --run-file "$RUN_FILE" --max-usd "$RUN_BUDGET" 2>/dev/null)" || bc_rc=$?
    if [ "$bc_rc" = 3 ]; then
      STOP_REASON="run budget exhausted (recorded \$$run_total of \$$RUN_BUDGET cap)"
      echo "  ✋ RUN BUDGET: recorded pass spend \$$run_total exceeds LOOP_MAX_RUN_BUDGET_USD=\$$RUN_BUDGET — stopping cleanly."
      break
    fi
  fi

  if [ "$SLEEP_FOR" != "0" ]; then sleep "$SLEEP_FOR"; fi
done

[ -z "$PICK" ] && STOP_REASON="${STOP_REASON:-backlog drained — no READY item left}"
[ -z "$PICK" ] && echo "✓ Backlog drained — no READY item left. Stopping."
# EXIT trap prints the run summary.
