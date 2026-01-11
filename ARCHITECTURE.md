# Dexter Architecture & Evolution Guide

This document provides a technical overview of Dexter's architecture for new engineers and outlines the critical path for future system hardening and improvements.

---

## 1. System Architecture

Dexter is an autonomous financial research agent built on a multi-phase state machine.

### The Agent Loop (`src/agent/orchestrator.ts`)
The core execution follows a cyclic pattern designed to ensure data sufficiency before answering:

1.  **Understand Phase**: Analyzes the natural language query to extract intent, entities, and historical context.
2.  **Plan Phase**: Generates a structured list of dependent tasks.
3.  **Execute Phase**: Iterates through tasks using **Just-in-Time (JIT) Tool Selection**. The model decides which tool to use *at the moment of execution* based on the specific task requirements and prior findings.
4.  **Reflect Phase**: A critical validation step. The agent reviews gathered data against the original query. If gaps exist, it triggers a new planning iteration.
5.  **Answer Phase**: Synthesizes the final data into a streamed response for the user.

### Interface & UI (`src/cli.tsx`)
*   **Framework**: Built with **React 19** and **Ink**.
*   **State Management**: Uses React hooks (`useAgentExecution`) to bridge the gap between the headless agent logic and the terminal UI.
*   **Philosophy**: React manages the complex rendering of progress bars, task lists, and streaming answers, while the Agent remains the source of truth for execution state.

### Tooling & Context (`src/tools/`)
*   **Strict Typing**: All tools are defined using Zod schemas for rigorous validation of LLM inputs.
*   **Context Management**: `ToolContextManager` handles data persistence between tasks, allowing the agent to "remember" findings from Step 1 to use in Step 5.

---

## 2. Strategic Improvement Plan (Hardening Dexter)

To evolve Dexter from a research prototype to a production-grade financial instrument, the following improvements are prioritized:

### A. Headless Logic Decoupling
*   **Problem**: Agent execution is currently tied to the React component lifecycle.
*   **Solution**: Refactor the `Agent` into a standalone, framework-agnostic class or worker. Use a pub/sub pattern (e.g., Zustand or RxJS) to let the UI subscribe to state updates. This enables headless testing and background execution.

### B. Deterministic Guardrails
*   **Problem**: Probabilistic LLM outputs can lead to "plan drift" or invalid tool calls.
*   **Solution**: Implement **Grammar-Constrained Sampling** or strict JSON-mode enforcement for the Planning and Tool Selection phases. Add a "Governor" layer to validate tool arguments against business logic *before* hitting APIs.

### C. "Ground Truth" Evaluation Pipeline
*   **Problem**: No objective way to measure accuracy or regression.
*   **Solution**: Establish a **Golden Dataset** of queries with verified answers. Implement automated backtesting in the CI/CD pipeline to ensure every code change maintains a <1% deviation from known financial truths.

### D. Intelligent Context Chunking
*   **Problem**: Financial filings (10-Ks) exceed context windows.
*   **Solution**: Move from "Read Full Filing" to a hierarchical retrieval strategy. The agent should first read a Table of Contents, then request specific sections, reducing noise and token costs.

### E. Executor Scope Restriction
*   **Problem**: Providing the LLM with too many tools at once increases the chance of selecting the wrong one.
*   **Solution**: Implement **Dynamic Tool Scoping**. When executing a "Financial Metric" task, the environment should only expose finance-related tools, hiding general search or utility tools to force higher precision.
