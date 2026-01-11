# Dexter (dexter-ts) - Developer Context

## Project Overview
**Dexter** is a sophisticated CLI-based AI agent designed for deep financial research. It utilizes a multi-phase agentic architecture (Understand → Plan → Execute → Reflect → Answer) to autonomously decompose complex financial queries, gather real-time data, and synthesize comprehensive reports.

The project is built with **TypeScript** and runs on the **Bun** runtime, featuring a rich terminal user interface (TUI) powered by **React** and **Ink**.

## Architecture

The core of Dexter is an iterative orchestration loop managed by the `Agent` class in `src/agent/orchestrator.ts`.

### The Agent Loop
1.  **Understand Phase** (`src/agent/phases/understand.ts`): Analyzes the user's natural language query to extract intent and entities.
2.  **Plan Phase** (`src/agent/phases/plan.ts`): Decomposes the query into a structured list of tasks with dependencies.
3.  **Execute Phase** (`src/agent/phases/execute.ts`):
    *   Iterates through planned tasks.
    *   Uses **Task Executor** (`src/agent/task-executor.ts`) to manage execution.
    *   Uses **Tool Executor** (`src/agent/tool-executor.ts`) to invoke specific tools (finance APIs, search, etc.) just-in-time.
4.  **Reflect Phase** (`src/agent/phases/reflect.ts`): Evaluates the results. If data is insufficient, it triggers a new planning iteration (looping back to Plan/Execute).
5.  **Answer Phase** (`src/agent/phases/answer.ts`): Synthesizes all gathered data into a final streamed response.

### Key Components
-   **CLI Entry**: `src/index.tsx` (shebang bin) wraps `src/cli.tsx` (React/Ink root).
-   **State Management**: `src/agent/state.ts` defines the shape of Plans, Tasks, and Results.
-   **Tools**: Located in `src/tools/`. Grouped by domain (e.g., `finance/`, `search/`).
-   **Context**: `ToolContextManager` (`src/utils/context.ts`) manages persistent context and tool outputs.

## Tech Stack
-   **Runtime**: [Bun](https://bun.sh)
-   **Language**: TypeScript (ESM)
-   **UI Framework**: React 19 + [Ink](https://github.com/vadimdemedes/ink) (Terminal UI)
-   **AI Orchestration**: LangChain.js
-   **Validation**: Zod
-   **Testing**: Bun Test

## Project Structure

```text
/src
├── index.tsx           # Entry point (bun executable)
├── cli.tsx             # Main React CLI component & state management
├── agent/              # Core Agent Logic
│   ├── orchestrator.ts # Main Agent class & loop logic
│   ├── phases/         # Implementation of agent phases
│   ├── state.ts        # TypeScript interfaces for agent state
│   ├── task-executor.ts# Manages task execution flow
│   └── tool-executor.ts# Handles tool invocation
├── components/         # Ink UI Components (Views, Inputs, Status)
├── tools/              # Tool definitions
│   ├── finance/        # Financial data tools (prices, filings, metrics)
│   └── search/         # Web search tools (Tavily)
├── utils/              # Helpers (config, env, message history)
└── hooks/              # Custom React hooks for CLI logic
```

## Setup & Development

### Prerequisites
-   **Bun** (v1.0+)
-   **API Keys**: OpenAI/Anthropic/Google (LLM), Financial Datasets (Data), Tavily (Search).

### Key Commands

| Command | Description |
| :--- | :--- |
| `bun install` | Install dependencies |
| `bun start` | Run the CLI in interactive mode |
| `bun dev` | Run the CLI in watch mode (auto-restart on save) |
| `bun test` | Run the test suite |
| `bun typecheck` | Run TypeScript type checking (no emit) |

### Configuration
Environment variables are managed in `.env` (copied from `env.example`).
-   `FINANCIAL_DATASETS_API_KEY`: **Required** for financial data.
-   `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`: At least one is required.

## Development Conventions

-   **Style**: Functional React components with Hooks. Strict TypeScript.
-   **Imports**: Grouped and sorted. Use `.js` extensions for local ESM imports.
-   **Naming**: 
    -   Components: `PascalCase.tsx`
    -   Hooks: `useCamelCase.ts`
    -   Tools/Utils: `camelCase.ts` or `snake_case.ts` (if matching API).
-   **Testing**: Colocated tests in `__tests__` directories. Files named `*.test.ts`.
-   **Tools**: New tools should be added to `src/tools/` and registered in `src/tools/index.ts`. They must strictly adhere to the Zod schema definitions.
