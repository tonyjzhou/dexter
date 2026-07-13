#!/usr/bin/env bash
#
# Stop hook for the headless /next-todo loop (scripts/loop_next_todo.sh).
# Registered via scripts/loop_next_todo.settings.json -> hooks.Stop, so it ONLY
# loads in a loop pass (no interactive session loads that settings file).
#
# WHY: a one-shot `claude -p` pass gets NO callback when a run_in_background job
# finishes. An agent that launches a background backfill/probe/build and then
# YIELDS its turn to "await the completion notification" silently abandons its
# work — the session ends, the bg job is killed, and any uncommitted edits are
# stranded. This is the recurring "bg-yield" trap (7 occurrences; see
# memory project_loop_next_todo_no_land_root_causes.md). The shell loop already
# DETECTS it after the fact (dirty tree -> `abandoned`), but detection is
# post-hoc. This hook is the structural PREVENTION the occ-3 lesson called for
# ("enforce execution-model invariants in the harness, not the prompt"): it
# blocks the agent from ending its turn while the tree is dirty, forcing it to
# finish synchronously (or commit its progress) in the SAME turn.
#
# SCOPE: blocks TWO end-of-turn yields, both of which end a headless pass with
# work that the loop cannot recover:
#   1. DIRTY-TREE abandonment (occurrences 3/4/7) — uncommitted edits stranded.
#   2. CLEAN-TREE bg-yield (occurrence 6, and the `make test` stall that
#      terminated a whole overnight drain) — a run_in_background verification/
#      probe/backfill whose completion callback headless `claude -p` NEVER
#      delivers, narrated as "I'll await the completion notification". This case
#      leaves a CLEAN tree, so the dirty check can't see it; it is detected from
#      the SESSION TRANSCRIPT (the last assistant turn backgrounded a Bash job or
#      narrates awaiting a callback). Best-effort + fail-open: no transcript /
#      unreadable / no match -> allow, so a legitimately-finished pass is never
#      blocked and the loop's `stalled` result-marker + its AUTO-PARK backstop
#      remain the recovery net for whatever this misses. High precision over
#      high recall — the transcript markers are the unambiguous callback phrases,
#      so a normal "done, committed and pushed" turn never trips it.
#
# CONTRACT (Claude Code Stop hook): stdin = JSON {cwd, session_id,
# permission_mode, transcript_path, ...}. To block, print {"decision":"block","reason":...} to
# stdout and exit 0. This Claude Code version exposes NO stop_hook_active field,
# so we implement our own re-entrancy guard (one block per session) to avoid an
# infinite block loop in headless mode.
#
# FAIL OPEN: any error here MUST allow the stop (exit 0, no stdout). A hook bug
# must never wedge a loop pass.

set +e  # never abort; fail open on every path

INPUT="$(cat 2>/dev/null)"

# --- parse stdin (python3 always present here; macOS bash is 3.2, so no mapfile) ---
_FIELDS=()
while IFS= read -r _line; do
  _FIELDS+=("$_line")
done < <(printf '%s' "$INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get("cwd") or "")
print(d.get("session_id") or "nosession")
print(d.get("permission_mode") or "")
print(d.get("transcript_path") or "")
' 2>/dev/null)

CWD="${_FIELDS[0]:-}"
SESSION_ID="${_FIELDS[1]:-nosession}"
PERM_MODE="${_FIELDS[2]:-}"
TRANSCRIPT="${_FIELDS[3]:-}"

# Interactive safety: the loop runs auto/bypassPermissions; a normal interactive
# session is "default". Never block interactive work. Empty (parse failed) also
# fails open.
case "$PERM_MODE" in
  default | "") exit 0 ;;
esac

# Resolve the repo dir; fall back to CLAUDE_PROJECT_DIR or PWD.
REPO="${CWD:-${CLAUDE_PROJECT_DIR:-$PWD}}"
[ -d "$REPO" ] || exit 0

DIRTY="$(git -C "$REPO" status --porcelain 2>/dev/null)"

# Clean-tree bg-yield detection. A run_in_background verification/probe/backfill whose completion
# callback headless never delivers ("I'll await the completion notification…") leaves a CLEAN tree and
# so slips past the dirty check — the exact stall that once terminated a whole overnight drain. Detect
# it from the session TRANSCRIPT: the LAST assistant turn either backgrounded a Bash job or narrates
# awaiting a callback. Best-effort + fail-open — no transcript / unreadable / no match -> "" (the hook
# then behaves exactly as before: a clean tree allows). A legitimately-finished "done, committed and
# pushed" turn does NOT match the callback-specific markers, so it is never falsely blocked.
# The detector program lives in a variable (heredoc read via `read -d ''`, NOT a heredoc inside
# `$(...)` — that nesting breaks the bash parser), then runs via `python3 -c`. Quoted delimiter
# ('PY') so the body is literal — the inner quotes/apostrophes in the marker list are safe.
read -r -d '' _BG_YIELD_PY <<'PY' || true
import sys, json
# Unambiguous "awaiting a background-job callback" narration only — NOT broad phrases like "in the
# background", which a completed pass could use innocently. Matched case-insensitively as substrings.
MARKERS = (
    "completion notification", "completion callback", "await its completion",
    "await the completion", "awaiting its completion", "awaiting the completion",
    "awaiting completion", "rather than poll", "i'll await", "i will await",
    "re-invoke me", "reinvoke me", "notification re-invoke", "wait for it to complete",
    "wait for it to finish", "self-reports when done", "self report when done",
)
def parts(obj):
    m = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    return m.get("content"), (m.get("role") or obj.get("type") or "")
last_text, backgrounded = "", False
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            content, role = parts(obj)
            if role != "assistant":
                continue
            texts, bg = [], False
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and isinstance(b.get("text"), str):
                        texts.append(b["text"])
                    if b.get("type") == "tool_use" and b.get("name") == "Bash":
                        if (b.get("input") or {}).get("run_in_background") is True:
                            bg = True
            elif isinstance(content, str):
                texts.append(content)
            # Track the LATEST assistant turn's signals (overwrite as we walk forward).
            if texts:
                last_text = "\n".join(texts)
            backgrounded = bg
except Exception:
    sys.exit(0)
low = last_text.lower()
if backgrounded:
    print("the final turn backgrounded a Bash job (run_in_background)")
elif any(m in low for m in MARKERS):
    print("the final turn narrates awaiting a background-job completion callback")
PY
BG_YIELD=""
if [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ]; then
  BG_YIELD="$(python3 -c "$_BG_YIELD_PY" "$TRANSCRIPT" 2>/dev/null)"
fi

# Nothing to prevent: a CLEAN tree with no background-job yield is a legitimately-finished pass (or a
# clean-tree yield the transcript couldn't confirm) — allow. Recovery for anything missed here still
# rides the loop's `stalled` marker + auto-park backstop.
if [ -z "$DIRTY" ] && [ -z "$BG_YIELD" ]; then
  exit 0
fi

# Re-entrancy guard: block at most ONCE per session. After one forced
# continuation, allow the stop — the shell loop's post-pass porcelain check then
# flags it ABANDONED (defense in depth), and we never spin.
GUARD_DIR="${TMPDIR:-/tmp}/.loop_stop_guard"
mkdir -p "$GUARD_DIR" 2>/dev/null
find "$GUARD_DIR" -type f -mmin +1440 -delete 2>/dev/null  # best-effort: drop stale markers
MARKER="$GUARD_DIR/${SESSION_ID//[^A-Za-z0-9_.-]/_}"
[ -e "$MARKER" ] && exit 0
touch "$MARKER" 2>/dev/null

# Build the block reason. A DIRTY tree (stranded EDITS) is the higher-signal case and takes
# precedence; a clean tree with a bg-yield signal is the verification/probe/backfill stall.
if [ -n "$DIRTY" ]; then
  FILES="$(printf '%s\n' "$DIRTY" | head -n 12 | sed 's/^/  /')"
  REASON="HEADLESS NO-YIELD GUARD (loop_next_todo): you are ending your turn with an UNCOMMITTED working tree. In a one-shot headless claude -p pass this means abandoned work the loop cannot recover. If you launched a run_in_background job and are waiting for its completion notification: that callback NEVER arrives in headless mode — the session ends here and the background job is killed. Do ONE of these NOW, in this same turn:
  1. Finish synchronously — re-run any backfill/probe/build/verification in the FOREGROUND (NOT run_in_background), then commit + push to main.
  2. If the work is already complete, commit + push it (a clean tree is the loop's land signal).
  3. If you genuinely cannot finish, commit your progress with a WIP message so it is recoverable, then stop.
Uncommitted files:
$FILES
(This guard fires once per pass; stop again still-dirty and the loop will flag it ABANDONED.)"
else
  REASON="HEADLESS NO-YIELD GUARD (loop_next_todo): you are ending your turn while a BACKGROUND JOB is what you are waiting on — $BG_YIELD. In a one-shot headless claude -p pass that completion callback NEVER arrives: the session ends here, the run_in_background job is killed, and this item lands NOTHING (the loop will auto-park it as stalled). The tree is clean, so there is no work to recover — the WORK ITSELF was thrown away. Do ONE of these NOW, in this same turn:
  1. Re-run the op in the FOREGROUND (NOT run_in_background) — block on it, read its real result, then commit + push / strike the item.
  2. If it is a long verification you cannot finish in-turn, say so plainly and stop WITHOUT waiting for a callback — the loop will PARK this item (trigger-gated) and drain past it.
(This guard fires once per pass; stop again and the loop's stalled-marker + auto-park backstop take over.)"
fi

python3 -c 'import json, sys; print(json.dumps({"decision": "block", "reason": sys.argv[1]}))' "$REASON" 2>/dev/null

exit 0
