# Repository Guidelines

## Project Structure & Module Organization
- `src/` contains the TypeScript CLI codebase (ESM).
- `src/agent/` holds the multi-phase orchestration logic; `src/tools/` contains finance and search tools; `src/components/` and `src/hooks/` power the Ink UI.
- Entry points are `src/index.tsx` (bin) and `src/cli.tsx` (CLI wiring).
- Tests live in `src/**/__tests__/` with `*.test.ts` files.

## Build, Test, and Development Commands
- `bun install`: install dependencies.
- `bun start`: run the CLI in interactive mode.
- `bun dev`: run the CLI with watch mode (auto-reload).
- `bun typecheck`: TypeScript type check without emitting.
- `bun test` / `bun test --watch`: run the Bun test suite (all or watch).

## Coding Style & Naming Conventions
- TypeScript with strict mode; ESM imports include `.js` extensions.
- Use 2-space indentation and keep imports grouped and sorted by path depth.
- Components use `PascalCase` filenames (e.g., `src/components/AgentProgressView.tsx`), hooks use `useX` naming in `src/hooks/`.
- Tools are grouped by domain (`src/tools/finance/`, `src/tools/search/`); snake_case filenames are acceptable for API-aligned modules (e.g., `insider_trades.ts`).
- Path alias `@/*` maps to `src/*`.

## Testing Guidelines
- Tests use `bun:test` and live under `__tests__` directories.
- Name tests `*.test.ts` and keep them colocated with the feature area.
- There is no explicit coverage threshold; add tests for new behavior and regression fixes.

## Commit & Pull Request Guidelines
- Commit messages are short, imperative, sentence case (e.g., "Add X", "Clean up Y", "Make Z faster").
- Keep pull requests small and focused; include a clear description, test notes, and screenshots for UI changes when helpful.

## Configuration & Secrets
- Copy `env.example` to `.env` and set API keys. `FINANCIAL_DATASETS_API_KEY` is required, plus at least one LLM provider key.
- Optional keys include `TAVILY_API_KEY` and `LANGSMITH_*` for search and tracing.
- Runtime context is written to `.dexter/` and is ignored by git.

## Additional References
- `CLAUDE.md` documents architecture details and the agent pipeline if you need a deeper overview.
