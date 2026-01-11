# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dexter is an autonomous financial research agent that uses LLMs to conduct deep financial analysis. It's a CLI application built with TypeScript, React + Ink for terminal UI, and LangChain.js for multi-provider LLM support.

## Commands

```bash
bun install          # Install dependencies
bun start            # Run the CLI
bun dev              # Run with watch mode (auto-reload)
bun typecheck        # Type check without emitting
bun test             # Run tests
bun test --watch     # Run tests in watch mode
```

## Architecture

### Agent Pipeline (5 Phases)

The core agent (`src/agent/orchestrator.ts`) runs queries through a multi-phase pipeline:

```
Query → UNDERSTAND → PLAN → EXECUTE → REFLECT → ANSWER
                       ↑       ↓
                       └───────┘ (iterate if incomplete, max 5)
```

1. **Understand** (`phases/understand.ts`): Extract intent and entities (tickers, dates, metrics)
2. **Plan** (`phases/plan.ts`): Decompose into tasks with dependencies. Each task is `use_tools` or `reason`
3. **Execute** (`phases/execute.ts`): Run tasks using just-in-time tool selection. Independent tasks parallelize
4. **Reflect** (`phases/reflect.ts`): Evaluate data sufficiency. Loop back to PLAN if more data needed
5. **Answer** (`phases/answer.ts`): Synthesize final response (streams to terminal)

### Key Components

| Directory | Purpose |
|-----------|---------|
| `src/agent/` | Core orchestration, state types, prompts, schemas |
| `src/tools/finance/` | Financial data tools (18+ via Financial Datasets API) |
| `src/tools/search/` | Web search (Tavily) |
| `src/components/` | Ink/React terminal UI components |
| `src/hooks/` | React hooks for query queue, agent execution |
| `src/model/` | LLM provider factory (OpenAI, Anthropic, Google, Ollama) |
| `src/utils/` | Environment, config, context management |

### Tool Execution Flow

Tools are selected at runtime, not during planning:
1. `task-executor.ts` receives a task
2. For `use_tools` tasks: `tool-executor.ts` uses a fast model to select appropriate tools
3. Tool output is summarized and stored in `ToolContextManager`
4. Results feed into subsequent tasks or final answer

### LLM Providers

Configured in `src/model/llm.ts`. Switch providers at runtime via `/model` command in CLI.
- OpenAI (default)
- Anthropic (Claude)
- Google (Gemini)
- Ollama (local)

## Schemas and Types

- **Zod schemas** (`src/agent/schemas.ts`): Used for structured LLM output parsing
- **State types** (`src/agent/state.ts`): Core types for `Plan`, `Task`, `Understanding`, `ReflectionResult`
- **Tool types** (`src/tools/types.ts`): Tool definition interface

## Environment Variables

Required:
- `FINANCIAL_DATASETS_API_KEY` - For all financial data tools

At least one LLM provider:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`
- `OLLAMA_BASE_URL` (for local models)

Optional:
- `TAVILY_API_KEY` - For web search capability
- `LANGSMITH_*` - For tracing/observability

## Testing

Tests use Bun's test runner. Test files are colocated in `__tests__/` directories.

```bash
bun test                           # Run all tests
bun test src/agent/__tests__/      # Run agent tests only
bun test --watch                   # Watch mode
```

## Code Patterns

- **Path alias**: `@/*` maps to `./src/*`
- **JSX runtime**: `react-jsx` (no React import needed)
- **Strict TypeScript**: Enabled with strict null checks
- **ESM**: Native ES modules throughout (`.js` extensions in imports)
