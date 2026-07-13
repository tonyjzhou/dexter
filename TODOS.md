# TODOS

Priority reference:
- **P0** — ship blocker, production bug, data loss risk
- **P1** — important, do next sprint
- **P2** — nice to have
- **P3** — backlog
- **P4** — someday/maybe

Format contract (parsed by `scripts/next_todo.py` — the deterministic selector behind
`/next-todo` and `scripts/loop_next_todo.sh`):
- `## ` headings are SECTIONS (grouping only); each tracked item is a `### ` heading
  with a `**Priority:** P0..P4` line in its body.
- A hard dependency is a `**Depends on:** …` line → the item buckets BLOCKED until met;
  `**Depends on:** none (…)` / `n/a` means no real dependency → READY.
- `trigger-gated` or `SHELVED` on the title/Priority line → PARKED (never auto-picked).
- Closed items: strike the title (`### ~~Title~~ — DONE`) or add a `**Completed:**` line.
- Avoid standalone full-line `**bold**` lines inside a body — the parser treats them as
  new item titles and truncates the previous item's body there (labelled lines like
  `**Why:** …` are safe).

---

## Backlog

### Add bun tests for the untested agent core, starting with scratchpad.ts
**Priority:** P2 | **Effort:** M
**Depends on:** none

**What:** `src/agent/` — the core of the product (agent.ts loop, scratchpad.ts, compact.ts, microcompact.ts, token-counter.ts, tool-executor.ts) — has ZERO test files, while the repo's 10 existing test files all cover gateway/tools/utils/controllers. Start with `src/agent/scratchpad.test.ts` (colocated, `bun:test`): append-only JSONL persistence to `.dexter/scratchpad/`, the token-threshold in-memory clearing (JSONL file never modified), and tool-call soft-limit tracking. Then token-counter.ts and compact.ts if budget allows.

**Why:** CLAUDE.md calls the scratchpad the "single source of truth for all agent work per query", and jest.config.js's `collectCoverageFrom` even targets `src/agent/**` — the coverage intent existed but the tests were never written. Any regression in scratchpad clearing silently corrupts every downstream answer's context.

**Done when:** `src/agent/scratchpad.test.ts` exists covering persistence + threshold-clear behavior, and `bun test` is green.

### Fix doc drift: CLAUDE.md and AGENTS.md contradict the actual code
**Priority:** P3 | **Effort:** S
**Depends on:** none

**What:** CLAUDE.md says the default model is `gpt-5.2`; actual is `DEFAULT_MODEL = 'gpt-5.5'` (src/model/llm.ts:19). AGENTS.md still says the TUI is "Ink (React for CLI)" with entry `src/cli.tsx`; actual is `@mariozechner/pi-tui` with `src/cli.ts` (no `ink` dependency in package.json — upstream migrated and the doc lagged).

**Why:** Both files are agent-onboarding docs — every Claude/Codex session in this repo starts by trusting them, and each drifted claim seeds wrong assumptions (wrong default model, wrong UI framework, wrong entry file).

**Context:** CLAUDE.md is fork-local (commit bc4c9cb) — edit freely. AGENTS.md is upstream-owned (this fork merges `virattt:main`) — keep that patch minimal to limit merge friction, or PR it upstream.

**Done when:** `grep -n 'gpt-5.2' CLAUDE.md` is empty and AGENTS.md no longer claims Ink or `src/cli.tsx`; `bun run typecheck` untouched (docs-only).

### Remove the dead legacy Jest toolchain
**Priority:** P3 | **Effort:** S
**Depends on:** none

**What:** `jest.config.js`'s `testMatch` is `**/__tests__/**/*.test.ts`, but no `__tests__/` directory exists anywhere in the repo — Jest cannot run a single test. All 10 real test files import from `bun:test`. Delete jest.config.js and the dead devDependencies: `jest`, `ts-jest`, `babel-jest`, `@babel/core`, `@babel/preset-env`, `@types/jest` (verify nothing else imports them first — no .babelrc/babel.config.* exists).

**Why:** Six dead devDependencies inflate installs and mislead contributors about which runner is real; CLAUDE.md already declares "Bun test runner is primary; Jest config exists for legacy".

**Context:** Upstream (`virattt/dexter`) may still ship these files — on future upstream merges keep the deletion (take "ours") or re-delete; alternatively PR the cleanup upstream.

**Done when:** jest.config.js is gone, the six devDeps are removed from package.json, and `bun install && bun run typecheck && bun test` are all green.

## Done
