---
name: next-todo
description: >
  Pick up the next-highest-priority, dependency-free item in TODOS.md and drive it end to end, then
  close it out. Invoke on "work the next todo", "pick up the next item", "what should I work on
  next", "next backlog item", "drain TODOS", or "/next-todo". This is the DEXTER specialization
  of the generic /next-todo (at ~/.claude/skills/next-todo/SKILL.md): same select → judgment →
  Track A/B → /goalify → build → verify → review → land (direct commit to main, no PR) →
  strike-DONE loop, with this repo's verify/build/land specifics layered on. Feeds every new review
  finding back into TODOS.md via /todoify. Do NOT invoke to land an already-built change (that is
  /git-triage) or to author a ticket (that is /todoify); it CHOOSES the item and orchestrates the
  named skills.
---

# Work the next backlog item, end to end — Dexter deltas

**Read the generic skill first:** `~/.claude/skills/next-todo/SKILL.md` owns the core loop (Step 1
select → Step 2 judgment → Step 3 Track A/B → Step 4 pipeline → Step 5 close → loop → traps). This
file only carries what is specific to **Dexter** — apply these on top. The headless rules below
were earned in value-hunt's drain history (17+ no-land root causes); they are inherited doctrine,
not speculation.

## Repo shape (affects every step)

This repo is a **fork of virattt/dexter** that periodically merges upstream `virattt:main`.
Keep diffs to upstream-owned files (AGENTS.md, `src/**`, jest/package config) as small as the
ticket allows to limit merge friction; fork-local files (CLAUDE.md, TODOS.md, `scripts/loop_*`,
`scripts/next_todo.py`, `tests/scripts/`, `.claude/`, Makefile) are free. Runtime is **Bun only**
(never npm/yarn/pnpm); TypeScript ESM strict; tests colocated as `*.test.ts` using `bun:test`.

## Step 1 — Select

This repo ships its own `scripts/next_todo.py`, so the generic selector's fallback uses it
automatically:
```bash
python3 scripts/next_todo.py          # human buckets: READY / BLOCKED / PARKED
python3 scripts/next_todo.py --json   # machine-readable; .next is the default pick
```
The TODOS.md grammar contract is at the top of TODOS.md itself.

## Step 2 — Judgment

- **Park-on-decline = write it back, verify, direct to main, SAME pass.** When you decline the top
  READY pick as deferred (only after confirming no *wanted* slice is buildable today), add a literal
  `**Depends on:** …` line (buckets the row BLOCKED) or append
  `(trigger-gated: … — <one-line reason>)` to its `**Priority:**` line (buckets it PARKED), commit
  straight to `main` as a one-line `docs(todos):` edit — no branch, no PR — then
  **re-run `python3 scripts/next_todo.py` and confirm the row moved buckets** before touching the
  next item. A headless pass has no callback: an unwritten or unverified decline is re-offered next
  pass and spins the loop.

## Step 3 — Track examples

- **Track A (invent):** a new subsystem (a new gateway channel beyond WhatsApp, a new tool family
  in `src/tools/`, a new eval harness capability). Shape first (`/office-hours` → optionally
  `/spec` / `/ap`).
- **Track B (finish):** test-coverage gaps, doc-drift fixes, dead-dependency cleanup, config
  surfacing. Skip shaping — proceed.
- **Headless Track-A decision-fork → BUILD-your-recommendation or PARK, NEVER escalate-and-stop.**
  A headless `claude -p` pass has no `AskUserQuestion` and no operator to reply, so an A/B/C
  decision-brief is a DEAD STOP: the turn ends with no commit and every other READY item idles
  behind the pick. If you have a confident default ("(A) … my recommendation"), TAKE IT and build
  end-to-end this pass. Only if you genuinely cannot choose alone, PARK it exactly like
  Park-on-decline (preserve the shaping brief in the item body, park marker on the Priority line,
  `docs(todos):` commit to main, re-run the selector to confirm PARKED). An *interactive* session
  does the opposite — ask, get the decision, then build.

## Step 4 — Pipeline deltas (verify · build · land)

- **Verify is type-specific, not "tests pass":**
  - TypeScript changes → `bun run typecheck` (tsc, green baseline) THEN `bun test` (bun:test,
    74-test green baseline, <1s). Cheapest first; stop at first red.
  - There is NO lint script in this repo — never invent one as a gate.
  - **Evals are NOT a gate:** `bun run src/evals/run.ts` burns real LLM API calls (LangSmith +
    provider keys). Never run evals in a headless loop pass.
  - Interactive TUI (`src/cli.ts`, pi-tui components) and the WhatsApp gateway need a terminal /
    a linked device — headless passes verify these via colocated `bun:test` tests only, plus a
    one-line manual-check note in the commit body when live behavior can't be exercised.
  - Loop harness (`scripts/*.py`, `scripts/loop_*.sh`, `tests/scripts/`) → `make loop-test`
    (pytest, 294 tests, ~25s).
- **Review → feed-back:** `/code-review` (or `/fr` for a large vertical). Append every deferred P#
  finding back into TODOS.md via **`/todoify`**.
- **Land — DIRECT TO MAIN, no PR (solo fork).** Commit + push straight to `main`: no feature
  branch, no PR. This repo has **no VERSION or CHANGELOG files** — do not create them. Releases
  are CalVer tags cut via `bash scripts/release.sh`, user-initiated only — NEVER run release.sh in
  a loop pass. Always push right after committing; if the push is rejected because the remote
  moved, `git pull --rebase && git push`.
- **Gate trailer — persist the item's check on the landing commit.** Add a commit trailer
  `Gate: <one cheap deterministic shell command>` carrying the item's completion check, e.g.
  `Gate: bun test` or `Gate: bun run typecheck` or `Gate: python3 -m pytest tests/scripts -q`.
  Constraints: a single command, runnable from the repo root, no live server/API-key dependency
  (so never evals), <~2 min, never a slash-command. PREFER a gate already written on the TODOS
  ticket (an outside-authored oracle) over one you authored this pass. `loop_next_todo.sh` re-runs
  the trailer after the landing: red → the run stops loudly as `gate-fail`. Docs-only commits are
  exempt.
- **Headless no-yield guard — ALL phases.** Inside `scripts/loop_next_todo.sh` you are a headless
  `claude -p` pass with NO turn continuation and NO background-job completion callback — at any
  phase. **NEVER end your turn while work you intend to finish is pending, and NEVER launch a
  `run_in_background` job and yield expecting to be re-invoked** — the callback never comes, the
  session ends, and the job is killed. This applies to investigation/verification probes as much as
  builds and lands: run EVERY long step synchronously/foreground within the SAME turn. The
  structural backstops (the Stop hook, the dirty-tree `abandoned` stop, the `stalled` classifier +
  auto-park) name the failure — they don't un-abandon the turn.

## Step 5 — Close (Dexter convention)

Strike with the landing commit: `### ~~<title>~~ — DONE (<short-sha>)` — no VERSION number exists
in this repo. Confirm new review findings landed as TODOS entries via /todoify.
