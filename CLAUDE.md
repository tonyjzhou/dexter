# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Dexter?

Dexter is an autonomous CLI-based AI agent for deep financial research. Built with TypeScript, pi-tui (terminal UI framework), and LangChain. It takes financial questions, decomposes them into research steps, executes tools to gather data, and synthesizes answers with self-validation.

## Commands

```bash
bun install                          # Install dependencies (postinstall runs playwright install)
bun start                            # Run in interactive mode
bun dev                              # Run with watch mode
bun test                             # Run tests (Bun test runner)
bun run typecheck                    # TypeScript type checking
bun run src/evals/run.ts             # Run eval suite (all questions)
bun run src/evals/run.ts --sample 10 # Run eval suite (random sample)
bun run gateway                      # Start WhatsApp gateway
bun run gateway:login                # Link WhatsApp (QR scan)
```

CI runs `bun run typecheck` and `bun test` on push/PR to main.

## Architecture

### Agent Loop (`src/agent/agent.ts`)
Core iterative loop: LLM call → tool execution → context update → repeat (max 10 iterations). When no tool calls are returned, generates a final answer in a separate LLM call using the full scratchpad context (no tools bound). Yields typed `AgentEvent`s (`tool_start`, `tool_end`, `thinking`, `answer_start`, `done`, etc.) consumed by the TUI for real-time updates.

### Scratchpad (`src/agent/scratchpad.ts`)
Single source of truth for all agent work per query. Append-only JSONL files persisted to `.dexter/scratchpad/`. Tracks tool results, thinking, and tool call limits (soft warnings, never hard blocks). Context management is Anthropic-style: full tool results kept in memory, oldest cleared when token threshold exceeded. JSONL file is never modified — clearing is in-memory only.

### Tool Registry (`src/tools/registry.ts`)
Tools are conditionally registered based on env vars. Each tool has a rich description (in `src/tools/descriptions/`) injected into the system prompt. Key tools:
- `financial_search` / `financial_metrics` / `read_filings` — financial data via Financial Datasets API
- `web_search` — Exa (preferred) → Perplexity → Tavily fallback chain based on which API key is set
- `web_fetch` / `browser` — web content (fetch for static, Playwright browser for JS-rendered pages)
- `read_file` / `write_file` / `edit_file` — filesystem tools
- `skill` — invokes SKILL.md-defined workflows (e.g., DCF valuation)

### Provider System (`src/providers.ts`, `src/model/llm.ts`)
`PROVIDERS` array is the single source of truth. Provider resolved by model name prefix (`claude-` → Anthropic, `gemini-` → Google, `grok-` → xAI, `ollama:` → Ollama, etc.). Default: OpenAI `gpt-5.2`. Each provider has a `fastModel` for lightweight tasks. Anthropic uses explicit `cache_control` on system prompt for ~90% input token savings.

### Skills (`src/skills/`)
SKILL.md files with YAML frontmatter (`name`, `description`) and markdown body. Discovered from three directories (later overrides earlier): builtin (`src/skills/`), user (`~/.dexter/skills/`), project (`.dexter/skills/`). Exposed as metadata in system prompt; LLM invokes via `skill` tool. Each skill runs at most once per query.

### TUI (`src/cli.ts`, `src/components/`)
Built with pi-tui (terminal UI library). `cli.ts` wires up controllers (agent runner, model selection, input history) and components (chat log, editor, working indicator). Uses a screen-based overlay pattern for model selection and tool approval flows.

### Gateway (`src/gateway/`)
WhatsApp integration via Baileys. Messages to yourself are routed through the agent. Has access control, session management, and channel-based architecture for future channel support.

### Controllers (`src/controllers/`)
MVC-style separation: `AgentRunnerController` manages agent lifecycle and approval flow, `ModelSelectionController` handles provider/model switching with settings persistence (`.dexter/settings.json`), `InputHistoryController` manages per-query history.

## Conventions

- **Runtime**: Always use Bun. Never npm/yarn/pnpm.
- **Language**: TypeScript ESM, strict mode. JSX via React (pi-tui for CLI rendering).
- **Path aliases**: `@/*` maps to `./src/*` (tsconfig paths).
- **Tests**: Colocated as `*.test.ts`. Bun test runner is primary; Jest config exists for legacy.
- **Versioning**: CalVer `YYYY.M.D` (no zero-padding). Release via `bash scripts/release.sh [version]`.
- **Config**: `.dexter/settings.json` (gitignored) for persisted model/provider selection.
- **Env**: `.env` for API keys (see `env.example`). Never commit real keys.
- **Adding a provider**: Add one entry to `PROVIDERS` in `src/providers.ts` and one factory to `MODEL_FACTORIES` in `src/model/llm.ts`.
- **Adding a tool**: Create tool in `src/tools/`, add rich description in `src/tools/descriptions/`, register in `src/tools/registry.ts`.
- **Adding a skill**: Create `<name>/SKILL.md` in any skill directory with YAML frontmatter.
