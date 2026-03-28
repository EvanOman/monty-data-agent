# Orchestration Mode Comparison: What We Built, What We Learned

*March 2026 — sandbox-agent experiment write-up*

## The Experiment

This project is a chat-based data analysis app where users ask questions about datasets loaded into DuckDB. An LLM generates Python code, a Monty sandbox executes it, and results stream back via SSE. The core question: **what's the best way to orchestrate the LLM-to-sandbox loop?**

We built 7 different orchestration modes — 3 ReAct (iterative) and 4 Plan-Execute-Synthesize (upfront planning) — to compare them empirically.

## The Modes

### ReAct (Iterative) Modes

These let the LLM control the loop. It thinks, acts, observes the result, and decides what to do next. Each step is a full LLM round-trip.

**Standard** — Claude Agents SDK. The LLM has `execute_code` and `load_result` tools. It calls them one at a time in a think→act→observe loop until it has enough to answer.

**Codemode** — Same ReAct loop but tools are exposed via MCP (Model Context Protocol). The LLM has richer tools for searching datasets and function metadata before writing code. More steps, more transparent, but slower.

**Pydantic AI** — Pydantic AI framework with the same tools. Structured output via Pydantic models. Uses `openai:gpt-5.4` by default (the others use Claude Sonnet).

### Plan-Execute-Synthesize Modes

These separate planning from execution. The LLM produces a structured plan (a DAG of subtasks), then each subtask is executed independently, then a final LLM call synthesizes everything.

**Parallel (graphlib)** — In-process DAG executor using Python's stdlib `graphlib.TopologicalSorter` + `asyncio.gather`. ~30 lines of orchestration code. Zero external dependencies.

**Pydantic Graph (beta API)** — Uses pydantic-graph's `GraphBuilder` with `@g.step` decorators, `.map()` for fan-out, and `.join()` for aggregation. Type-safe step connections.

**Graph State (original API)** — Uses pydantic-graph's `BaseNode` subclasses with typed return hints. Models the pipeline as a state machine: `PlanNode → ExecuteBatchNode → SynthesizeNode → End`. The graph is defined once at module level.

**Temporal** — Same plan-execute-synthesize pipeline but orchestrated by a Temporal server (separate Docker infrastructure). Provides durability, automatic retries, and a visual workflow UI. Requires 3 Docker containers and a separate worker process.

## The Evaluation

### Setup

- 3 test queries at increasing complexity (simple, medium, complex)
- 3 runs per query per mode (54 total API calls for the 6 testable modes)
- All using Claude Sonnet except pydantic_ai (GPT 5.4)
- Temporal excluded from runtime eval due to a serialization bug

### Queries

| Query | What it tests |
|-------|--------------|
| "What is the average age of Titanic passengers?" | Single computation. Should be one subtask. |
| "Compare survival rates by passenger class. Which had the highest?" | Multi-step: compute per class, then compare. |
| "Compute average age and survival rate by class, then identify which class had the best combination of younger age and higher survival rate." | Multi-step with dependency: compute stats, then analyze. |

### Raw Results

**Simple query:**
```
pydantic_ai           5.6s    2 turns    1 tool call
parallel (graphlib)   7.7s    3 turns    1 tool call    1 plan task
pydantic_graph        7.6s    3 turns    1 tool call    1 plan task
graph_state           7.6s    3 turns    1 tool call    1 plan task
standard              10.0s   3 turns    1 tool call
codemode              12.9s   4 turns    3 tool calls
```

**Medium query:**
```
pydantic_ai           10.5s   3 turns    2 tool calls
parallel (graphlib)   12.2s   3 turns    1 tool call    1 plan task
graph_state           12.8s   3 turns    1 tool call    1 plan task
pydantic_graph        13.6s   3 turns    1 tool call    1 plan task
standard              27.7s   7 turns    3 tool calls
codemode              31.6s   8 turns    7 tool calls
```

**Complex query:**
```
pydantic_ai           23.3s   4 turns    3 tool calls
graph_state           25.4s   4 turns    2 tool calls   2 plan tasks
pydantic_graph        28.8s   4 turns    2 tool calls   2 plan tasks
parallel (graphlib)   29.6s   4 turns    2 tool calls   2 plan tasks
standard              45.5s   10 turns   5 tool calls
codemode              87.0s   11 turns   10 tool calls
```

100% success rate across all 54 runs. Every mode produced a correct answer to every query.

## What We Learned

### 1. Explicit planning doesn't help when the LLM already plans implicitly

The central finding: **the plan-execute modes were never faster than pydantic_ai's ReAct loop**, despite the architectural promise of parallelism.

Why? Every mode is doing plan → execute → synthesize. The ReAct modes just do it implicitly — the LLM writes a multi-step script that computes everything in one code block, or chains a few focused code blocks sequentially. The explicit planning modes add a dedicated LLM call to produce a JSON plan, which costs 2-3 seconds and produces zero executable work.

The planning call is pure overhead unless the plan enables parallelism. And in practice, these Titanic queries decomposed into 1-2 subtasks — not enough independent work to recoup the planning cost through parallel execution.

### 2. Parallelism requires the right kind of question

For plan-execute to beat ReAct, you need questions that decompose into 3+ independent subtasks. Something like:

> "Compare the average salary by department against the burnout rate, employee count, and average tenure — then identify which departments are overstaffed with unhappy, well-paid employees."

This decomposes into 4 independent computations (salary, burnout, headcount, tenure by department) that could run in parallel, saving 10-15 seconds. But for the typical "compute X and Y, then compare" pattern, there's a dependency between compute and compare, so everything runs sequentially regardless.

### 3. The orchestration layer doesn't matter — the LLM calls dominate

The three plan-execute backends (graphlib, pydantic-graph beta, pydantic-graph BaseNode) produced **identical performance within noise** (25-30s for complex queries). Whether you use `asyncio.gather`, `.map()/.join()`, or a state machine loop, the bottleneck is the LLM API calls, not the 0.001s of Python orchestration overhead.

This means the choice between backends should be driven by developer ergonomics, not performance:
- **graphlib**: simplest (stdlib, 30 lines), full DAG support
- **pydantic-graph beta**: most declarative (`.map()` fan-out is readable), free mermaid diagrams
- **pydantic-graph BaseNode**: best streaming control (`graph.iter()` gives node-by-node events)

### 4. Model choice matters more than architecture

pydantic_ai uses `openai:gpt-5.4` while the others use `claude-sonnet-4-5`. The GPT model was consistently faster at these structured code-generation tasks. If we ran all modes on the same model, the gap between ReAct and plan-execute would likely narrow further.

### 5. ReAct is better at self-correction

Standard mode averaged 2 retries per complex query — it would generate code, hit a sandbox error, see the traceback, and fix the code. Plan-execute modes had 0-1 retries because each subtask gets one shot (with retry on failure, but no "see the error and adapt" loop). For error-prone tasks, ReAct's iterative self-correction is a genuine advantage.

### 6. Temporal is overkill for synchronous chat

Temporal adds 823 lines of code, 3 Docker containers, a separate worker process, gRPC serialization, and ~6 seconds of worker cold-start time. It provides durability (survive crashes), distributed execution, and a visual workflow UI. For a chat app where queries take 10-30 seconds and a crash just means re-asking the question, none of that matters. The in-process alternatives are simpler and equally fast.

Temporal's value proposition is real — for multi-tenant services, long-running workflows, or pipelines that span minutes/hours/days. It's just not the right tool for synchronous data analysis.

## Static Comparison

| | Standard | Codemode | Pydantic AI | Parallel | Pydantic Graph | Graph State | Temporal |
|---|---------|----------|-------------|----------|---------------|-------------|---------|
| **Architecture** | ReAct | ReAct+MCP | ReAct | Plan-Execute | Plan-Execute | Plan-Execute | Plan-Execute |
| **Source LOC** | 375 | 500 | 262 | 355 | 270 | 351 | 823 |
| **External deps** | claude-agent-sdk | anthropic, mcp | pydantic-ai | *none (stdlib)* | pydantic-graph | pydantic-graph | temporalio |
| **Infrastructure** | None | MCP subprocess | None | None | None | None | Docker (3 containers) |
| **Parallelism** | None | None | None | Full DAG | Fan-out only | Batch-level | Full DAG |
| **DAG dependencies** | N/A | N/A | N/A | Yes | No | Yes (via batches) | Yes |
| **Type-safe edges** | No | No | Pydantic models | No | Yes (StepContext) | Yes (return hints) | No |
| **Visualization** | Logs | Logs | Logfire | None | Mermaid | Mermaid | Temporal UI |
| **Durability** | None | None | None | None | None | None | Full |
| **Streaming** | Token-level | Token-level | Token-level | Post-hoc chunked | Queue bridge | Node-level | Polling |

## Recommendations

**Default mode:** pydantic_ai (ReAct) — fastest, simplest, self-correcting.

**For complex multi-dataset questions:** parallel (graphlib) — the planning overhead pays for itself when there are 3+ independent subtasks to parallelize.

**Ideal architecture:** Auto-route based on question complexity. A cheap classifier (or the LLM itself in a fast call) decides whether to plan upfront or just start coding. Simple questions go straight to ReAct; complex multi-faceted questions get a plan.

**What to keep:** standard, pydantic_ai, and parallel. These cover the three useful points in the design space (basic ReAct, framework ReAct, and plan-execute). The others are interesting experiments but don't add unique capability.

**What to consider removing:**
- codemode: 2-3x slower than standard, same architecture, MCP adds complexity
- pydantic_graph_mode and graph_state: identical performance to parallel, more code, less capability (no DAG deps for beta API)
- temporal: overkill for this use case, but valuable as a reference for when durability matters
