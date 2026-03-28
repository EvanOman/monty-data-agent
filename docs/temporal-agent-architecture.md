# Temporal-Based Agent Architecture

*An alternative orchestration model for sandbox-agent, inspired by Arcana's Plan-Execute-Synthesize pipeline.*

## Why Temporal?

The current sandbox-agent architecture runs a single-threaded agent loop: the LLM thinks, calls a tool, waits for results, thinks again, calls another tool — all sequentially within one async task. This is the standard ReAct pattern.

The limitation becomes clear when a user asks a multi-faceted question:

> "Compare the average salary by department against the burnout rate, and tell me which departments are overpaying for unhappy employees."

Today's flow:
1. LLM generates code to compute avg salary by department → execute → wait
2. LLM generates code to compute burnout rate by department → execute → wait
3. LLM generates code to join and compare → execute → wait
4. LLM synthesizes a response

Steps 1 and 2 are independent. They could run in parallel. But the ReAct loop doesn't support that — it's one thought, one action, one observation at a time.

Temporal solves this by making the orchestration explicit, durable, and parallel.

---

## What Temporal Is (and Isn't)

**Temporal is a workflow orchestration engine.** You define workflows as code (Python, Go, etc.) and Temporal manages:
- Executing steps (called "activities") in sequence or parallel
- Retrying failed steps with configurable backoff
- Resuming from exactly where it left off after crashes
- Tracking state across long-running or short-lived processes
- Enforcing timeouts at the step and workflow level

**Temporal is not:**
- A sandbox or code execution environment (that's still Monty's job)
- An LLM framework (that's still Anthropic/OpenAI SDK)
- A message queue (though it uses one internally)

It sits between the LLM and the tools — replacing the ad-hoc `for` loop in `client.py` with a structured, observable, fault-tolerant pipeline.

---

## Proposed Architecture: Plan-Execute-Synthesize

### Overview

```
User Question
     │
     ▼
┌─────────────┐
│   PLAN      │  Single LLM call: decompose question into sub-tasks
│             │  Output: list of independent sub-tasks + dependency graph
└─────┬───────┘
      │
      ▼
┌─────────────┐
│   EXECUTE   │  Temporal workflow: run sub-tasks as parallel activities
│             │  Each activity: LLM generates code → Monty executes → result
│  ┌───┐ ┌───┐│
│  │ A │ │ B ││  Independent tasks run in parallel
│  └─┬─┘ └─┬─┘│
│    │     │  │
│    ▼     ▼  │
│  ┌─────────┐│
│  │    C    ││  Dependent tasks wait for predecessors
│  └─────────┘│
└─────┬───────┘
      │
      ▼
┌─────────────┐
│  SYNTHESIZE │  Single LLM call: combine all sub-task results into
│             │  a coherent response, stream to user
└─────────────┘
```

### The Three Phases

**Phase 1: Plan**
- A single LLM call with a planning-specific system prompt
- Input: user question + dataset schemas + conversation history
- Output: structured JSON — a list of sub-tasks with:
  - `task_id`: unique identifier
  - `description`: what this sub-task should compute
  - `depends_on`: list of task_ids that must complete first (empty = can run immediately)
  - `datasets`: which datasets/tables are needed
- The plan is itself an artifact — saved, inspectable, replayable

Example output for the salary/burnout question:
```json
{
  "tasks": [
    {
      "task_id": "avg_salary",
      "description": "Compute average salary grouped by department",
      "depends_on": [],
      "datasets": ["employees"]
    },
    {
      "task_id": "burnout_rate",
      "description": "Compute average burnout_rate grouped by department",
      "depends_on": [],
      "datasets": ["employees"]
    },
    {
      "task_id": "comparison",
      "description": "Join avg_salary and burnout_rate results, compute ratio, flag departments where salary is above median but satisfaction is below median",
      "depends_on": ["avg_salary", "burnout_rate"],
      "datasets": []
    }
  ]
}
```

**Phase 2: Execute (Temporal Workflow)**
- A Temporal workflow receives the plan and executes it as a DAG
- Each sub-task is a Temporal activity that:
  1. Calls the LLM with the sub-task description + relevant dataset schemas
  2. Gets back Python code
  3. Executes in Monty sandbox
  4. Returns the classified result (table, scalar, dict, etc.)
- Independent tasks (no `depends_on`) run in parallel via `asyncio.gather` inside the workflow
- Dependent tasks wait for their predecessors, receiving predecessor results as additional context
- Each activity has its own timeout (inherits `MAX_MONTY_DURATION_SECS`) and retry policy
- If a sub-task fails, Temporal retries it (possibly with a modified prompt including the error)

**Phase 3: Synthesize**
- A single LLM call receives all sub-task results
- System prompt instructs it to combine findings into a coherent narrative
- Streams the response token-by-token to the user via the existing SSE infrastructure
- Can reference specific artifacts by UID

### Streaming During Execution

One of Arcana's key innovations: stream tokens to the user *while sub-agents are still running*. Here's how that would work:

1. **Plan phase** streams a brief status: "Breaking this into 3 parts..."
2. **Execute phase** emits SSE status events as each sub-task starts/completes:
   - `{"event": "status", "data": "Computing salary by department..."}`
   - `{"event": "artifact", "data": {...}}` (as each sub-task produces results)
   - `{"event": "status", "data": "Computing burnout rate..."}`
3. **Synthesize phase** streams the final narrative token-by-token

The user sees incremental progress rather than a loading spinner for 10 seconds.

---

## How It Maps to the Current Codebase

### What Changes

| Component | Current | With Temporal |
|---|---|---|
| Agent loop | `for` loop in `client.py` (sequential) | Temporal workflow (parallel DAG) |
| Tool calls | LLM decides one at a time | Planned upfront, executed in parallel |
| Retries | None (or LLM self-corrects next turn) | Temporal activity retries with backoff |
| State | In-memory (lost on crash) | Temporal server (durable, resumable) |
| Observability | Logs | Temporal UI shows workflow graph, timings, retries |
| Timeout handling | `MAX_MONTY_DURATION_SECS` per execution | Per-activity + per-workflow timeouts |

### What Stays the Same

- **Monty sandbox** — still executes all code. Activities call the same `execute_code()` function
- **DuckDB** — still the data layer. Sub-tasks query it the same way
- **SQLite artifacts** — still persists results. Each sub-task creates an artifact
- **SSE streaming** — still the delivery mechanism. Temporal workflow pushes events to the same queue
- **FastAPI app** — still the HTTP layer. The chat endpoint starts a Temporal workflow instead of an agent loop
- **System prompts** — adapted but same core content (dataset schemas, sandbox API docs)
- **Frontend** — unchanged. Events are the same shape

### New Components

```
src/sandbox_agent/
├── temporal/
│   ├── worker.py          — Temporal worker process (runs activities)
│   ├── workflows.py       — PlanExecuteSynthesize workflow definition
│   ├── activities.py      — plan(), execute_subtask(), synthesize()
│   ├── models.py          — SubTask, ExecutionPlan, SubTaskResult
│   └── prompts.py         — Planning and synthesis system prompts
```

---

## Implementation Sketch

### Workflow Definition

```python
# temporal/workflows.py
from temporalio import workflow
from datetime import timedelta

@workflow.defn
class PlanExecuteSynthesize:
    @workflow.run
    async def run(self, question: str, context: WorkflowContext) -> str:
        # Phase 1: Plan
        plan = await workflow.execute_activity(
            plan_subtasks,
            args=[question, context],
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Phase 2: Execute (parallel where possible)
        results = {}
        # Group tasks by dependency depth
        for batch in plan.batches():
            # batch = list of tasks whose dependencies are all satisfied
            batch_results = await asyncio.gather(*[
                workflow.execute_activity(
                    execute_subtask,
                    args=[task, results, context],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(
                        maximum_attempts=3,
                        backoff_coefficient=2.0,
                    ),
                )
                for task in batch
            ])
            for task, result in zip(batch, batch_results):
                results[task.task_id] = result

        # Phase 3: Synthesize
        response = await workflow.execute_activity(
            synthesize_results,
            args=[question, results, context],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return response
```

### Activity: Execute a Sub-Task

```python
# temporal/activities.py
from temporalio import activity

@activity.defn
async def execute_subtask(
    task: SubTask,
    predecessor_results: dict[str, SubTaskResult],
    context: WorkflowContext,
) -> SubTaskResult:
    # Build prompt with sub-task description + predecessor results
    prompt = build_subtask_prompt(task, predecessor_results, context)

    # LLM generates code
    response = await anthropic_client.messages.create(
        model=context.model,
        system=SUBTASK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    code = extract_code(response)

    # Execute in Monty (same function as today)
    result = await execute_code(
        code=code,
        duckdb=context.duckdb,
        sqlite=context.sqlite,
        timeout=context.max_duration,
    )

    # Emit progress event to SSE queue
    await context.event_queue.put(ChatEvent(
        kind="artifact",
        artifact=result.artifact,
    ))

    return SubTaskResult(
        task_id=task.task_id,
        artifact_uid=result.artifact.uid,
        summary=result.summary,
        result_type=result.result_type,
    )
```

### Infrastructure Requirements

```yaml
# docker-compose.yml addition
services:
  temporal:
    image: temporalio/auto-setup:latest
    ports:
      - "7233:7233"   # gRPC
      - "8233:8233"   # UI
    environment:
      - DB=sqlite     # lightweight for dev, postgres for prod

  temporal-ui:
    image: temporalio/ui:latest
    ports:
      - "8080:8080"
    environment:
      - TEMPORAL_ADDRESS=temporal:7233
```

The worker runs alongside the FastAPI app:
```python
# temporal/worker.py
async def run_worker():
    client = await Client.connect("localhost:7233")
    worker = Worker(
        client,
        task_queue="sandbox-agent",
        workflows=[PlanExecuteSynthesize],
        activities=[plan_subtasks, execute_subtask, synthesize_results],
    )
    await worker.run()
```

---

## What You Gain

### 1. Parallel Execution
The biggest win. A 3-subtask question that takes 15 seconds today (5s × 3 sequential) could take 7 seconds (5s for the slowest parallel branch + 2s for plan + synthesize). For complex questions that decompose into 4-5 sub-tasks, the speedup is significant.

### 2. Fault Tolerance
If one sub-task fails (Monty timeout, bad LLM output, DuckDB error), Temporal retries it automatically without restarting the entire pipeline. The current agent either burns an LLM turn self-correcting or gives the user a partial/broken response.

### 3. Observability
Temporal's UI shows every workflow execution as a visual graph: which activities ran, how long each took, which retried, what the inputs/outputs were. This is dramatically better than reading logs. You can inspect any historical workflow execution in full detail.

### 4. Structured Planning
The explicit plan phase means the system reasons about *what to compute* before computing anything. This often produces better results than ReAct's incremental "think → act → observe → think" because:
- The plan can identify independent sub-tasks upfront (enabling parallelism)
- The plan can allocate the right datasets to each sub-task
- The synthesis step has all results available simultaneously, enabling cross-referencing

### 5. Resumability
If the server crashes mid-workflow, Temporal picks up exactly where it left off when the worker restarts. No lost work. The current agent loop loses everything on crash.

---

## What You Lose (or What Gets Harder)

### 1. Infrastructure Complexity
Temporal requires a separate server process (or managed service). For a single-developer prototype, this is meaningful overhead:
- Docker Compose for the Temporal server
- A worker process alongside the FastAPI app
- Temporal client configuration
- New failure modes (Temporal server down, worker disconnected)

### 2. Adaptive Reasoning
ReAct's strength is that each step can react to the previous step's results. The plan-execute model commits to a plan upfront. If sub-task A reveals something unexpected that changes what sub-task B should do, the rigid plan can't adapt.

Mitigation: allow the synthesis step to trigger a follow-up workflow if the results are insufficient. Or implement a "re-plan" activity that runs after the first execute batch completes.

### 3. LLM Planning Quality
The plan is only as good as the LLM's ability to decompose the question. Simple questions ("what's the average salary?") don't benefit from planning — they're one sub-task. Planning adds latency for no gain. Complex questions need good decomposition, which isn't guaranteed.

Mitigation: use planning only for questions the LLM judges as multi-step. Simple questions bypass directly to execute.

### 4. Cold Start / Latency Floor
Temporal adds some baseline latency: workflow scheduling, activity dispatch, worker polling. For the "fast path" (simple question, one tool call), the current direct approach is faster.

### 5. Testing Complexity
Temporal workflows need their own test harness. The Temporal Python SDK provides `WorkflowEnvironment` for testing, but it's another layer of test infrastructure.

---

## Hybrid Approach: Best of Both

Rather than replacing the current architecture entirely, add Temporal as a **fourth mode** alongside standard, codemode, and pydantic_ai:

```python
# config.py
MODES = Literal["standard", "codemode", "pydantic_ai", "temporal"]
```

The routing logic in the chat endpoint could even be automatic:
- Simple questions (single dataset, straightforward query) → standard mode (fast, no planning overhead)
- Complex questions (multiple datasets, comparisons, multi-step analysis) → temporal mode (parallel execution, structured plan)

The LLM can make this routing decision in a cheap, fast call before the main pipeline starts. Or the user can select the mode manually, just like today.

---

## Comparison: Current vs. Temporal

| Dimension | Current (ReAct) | Temporal (Plan-Execute-Synthesize) |
|---|---|---|
| **Latency (simple question)** | ~3-5s | ~4-6s (planning overhead) |
| **Latency (complex question)** | ~15-25s (sequential) | ~7-12s (parallel) |
| **Fault recovery** | None — restart from scratch | Automatic retry per sub-task |
| **Observability** | Logs | Visual workflow graph + full history |
| **Adaptive reasoning** | Strong (each step reacts) | Weaker (committed to plan) |
| **Infrastructure** | None (in-process) | Temporal server + worker |
| **State durability** | In-memory (lost on crash) | Durable (survives crashes) |
| **Complexity** | Low | Medium-High |

---

## Next Steps (If Pursuing This)

1. **Add `temporalio` to dependencies** — `uv add temporalio`
2. **Docker Compose for Temporal dev server** — single container, SQLite backend
3. **Implement the plan activity** — this is the most important piece to get right. Test it with diverse questions to see if the LLM produces good DAGs.
4. **Wire up a single sequential workflow first** — plan → execute (one at a time) → synthesize. Validate the architecture works before adding parallelism.
5. **Add parallelism** — group independent tasks and execute with `asyncio.gather`.
6. **Stream events from activities** — connect the Temporal worker's event output to the existing SSE queue.
7. **Benchmark** — compare latency and answer quality between ReAct and Plan-Execute-Synthesize on a set of representative questions.
