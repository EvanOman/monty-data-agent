---
title: "feat: Finish Temporal Plan-Execute-Synthesize Mode"
type: feat
status: active
date: 2026-03-19
origin: docs/temporal-agent-architecture.md
---

# Finish Temporal Plan-Execute-Synthesize Mode

## Overview

The Temporal mode is ~80% scaffolded but has never been run end-to-end. All source files exist (models, workflows, activities, prompts, worker, client) and the mode is wired into routes, main, config, frontend, Justfile, and docker-compose. However, there are critical gaps that prevent it from actually working: no conversation history, no real streaming, broken artifact association, no error handling for partial failures, and zero tests.

This plan addresses every gap to bring the Temporal mode to feature parity with the existing agent modes.

## Problem Statement / Motivation

The architecture doc (`docs/temporal-agent-architecture.md`) lays out a compelling case: complex multi-step questions that take 15-25s sequentially could complete in 7-12s with parallel execution, plus fault tolerance, observability, and structured planning. The scaffolding is done — now it needs to actually work.

## Proposed Solution

Fix the gaps in priority order across 5 phases. Each phase produces a testable increment. The guiding principle is: **get it working end-to-end first (Phase 1-2), then make it robust (Phase 3), then make it polished (Phase 4-5).**

## Technical Approach

### Architecture

No architectural changes needed — the existing structure is sound. The fixes are all within the existing `src/sandbox_agent/temporal/` package plus minor touches to `config.py` and `docker-compose.temporal.yml`.

```
src/sandbox_agent/temporal/
├── __init__.py
├── __main__.py
├── activities.py      ← Fix: input dataclasses, heartbeating, shared Anthropic client, conversation_id
├── client.py          ← Fix: conversation history, progress polling, workflow timeout
├── models.py          ← Fix: add input/output dataclasses for activities
├── prompts.py         ← Fix: add conversation history to plan prompt
├── worker.py          ← Fix: max_concurrent_activities, shared Anthropic client
└── workflows.py       ← Fix: partial failure handling, query handler for progress
```

### Implementation Phases

#### Phase 1: Critical Fixes (Get It Working)

These fixes are required before the mode can be tested at all.

**1a. Add activity input/output dataclasses** (`models.py`)

Temporal best practice: single dataclass per activity input. This also solves the conversation_id threading problem.

```python
# models.py — new dataclasses

@dataclass
class PlanInput:
    question: str
    schema_context: str
    plan_system_prompt: str
    conversation_history: list[dict] = field(default_factory=list)  # last N messages

@dataclass
class ExecuteSubtaskInput:
    task_id: str
    description: str
    datasets: list[str]
    predecessor_summaries: dict[str, str]
    schema_context: str
    subtask_system_prompt: str
    conversation_id: str = ""  # for artifact association

@dataclass
class SynthesizeInput:
    question: str
    task_summaries: dict[str, str]
    synthesize_system_prompt: str
```

Update `activities.py` to accept these dataclasses instead of positional args. Update `workflows.py` call sites to construct them.

**1b. Fix conversation history** (`client.py`, `prompts.py`, `activities.py`)

- In `TemporalClient.chat()`: load conversation history from SQLite (same pattern as AgentClient/PydanticAIClient), take last 10 messages, serialize as `list[dict]`
- Pass history into `PlanInput`
- In `plan_subtasks` activity: format history into the user prompt so the planner has context for follow-up questions
- In `prompts.py`: update plan prompt template to include a `## Conversation History` section when history is non-empty

**1c. Fix artifact conversation association** (`activities.py`, `workflows.py`)

- Add `conversation_id` to the workflow args and `ExecuteSubtaskInput`
- In `execute_subtask`: use `input.conversation_id` instead of hardcoded `"temporal"`

**1d. Fix partial failure handling** (`workflows.py`)

```python
# Replace bare asyncio.gather with return_exceptions=True
raw_results = await asyncio.gather(
    *[coro for _, coro in batch_coros],
    return_exceptions=True,
)
for (task_id, _), result in zip(batch_coros, raw_results, strict=True):
    if isinstance(result, BaseException):
        all_results[task_id] = SubTaskResult(
            task_id=task_id,
            artifact_uid="",
            summary=f"Error: {result}",
            result_type="error",
            error=str(result),
        )
    else:
        all_results[task_id] = result
```

- Files: `workflows.py`
- Acceptance: a workflow with one failing subtask still completes and returns partial results

**1e. Add workflow timeout** (`client.py`)

```python
result = await client.execute_workflow(
    "PlanExecuteSynthesize",
    args=[...],
    id=workflow_id,
    task_queue=TASK_QUEUE,
    execution_timeout=timedelta(minutes=5),
)
```

- Files: `client.py`

#### Phase 2: Worker Hardening

**2a. Set max_concurrent_activities** (`worker.py`)

```python
worker = Worker(
    client,
    task_queue=TASK_QUEUE,
    workflows=[PlanExecuteSynthesize],
    activities=[plan_subtasks, execute_subtask, synthesize_results],
    max_concurrent_activities=5,
)
```

LLM workloads are expensive per-call. 5 concurrent activities balances parallelism with API rate limits.

**2b. Share Anthropic client across activities** (`worker.py`, `activities.py`)

Same pattern as `get_shared_stores()`:

```python
# worker.py
_anthropic_client: AsyncAnthropic | None = None

def get_shared_anthropic() -> AsyncAnthropic:
    if _anthropic_client is None:
        raise RuntimeError("Anthropic client not initialized")
    return _anthropic_client
```

Initialize in `run_worker()`. Replace `AsyncAnthropic()` calls in each activity with `get_shared_anthropic()`.

**2c. Add heartbeating to activities** (`activities.py`)

Add `heartbeat_timeout=timedelta(seconds=30)` to activity options in `workflows.py`. Add `activity.heartbeat()` calls in activities before and after LLM API calls:

```python
@activity.defn
async def execute_subtask(input: ExecuteSubtaskInput) -> SubTaskResult:
    activity.heartbeat("calling LLM...")
    response = await client.messages.create(...)
    activity.heartbeat("executing code...")
    result = await asyncio.to_thread(execute_code, ...)
    activity.heartbeat("saving artifact...")
    ...
```

**2d. Add TEMPORAL_MODEL config** (`config.py`)

```python
TEMPORAL_MODEL = os.environ.get("TEMPORAL_MODEL", MODEL)
```

Use in activities instead of `MODEL` directly.

#### Phase 3: Progress Streaming

Replace the blocking `execute_workflow` + fake chunking with a polling-based approach.

**3a. Add query handler to workflow** (`workflows.py`)

```python
@workflow.defn
class PlanExecuteSynthesize:
    def __init__(self):
        self._completed_tasks: list[dict] = []
        self._plan: list[dict] = []
        self._status: str = "planning"

    @workflow.query
    def get_progress(self) -> dict:
        return {
            "status": self._status,
            "plan": self._plan,
            "completed_tasks": self._completed_tasks,
        }
```

Update the workflow `run()` method to populate these fields as subtasks complete.

**3b. Poll workflow progress from client** (`client.py`)

Replace `execute_workflow` (which blocks) with `start_workflow` + polling:

```python
handle = await client.start_workflow(...)

# Poll for progress
last_seen = 0
while True:
    try:
        progress = await handle.query(PlanExecuteSynthesize.get_progress)
    except Exception:
        await asyncio.sleep(2)
        continue

    # Emit new completed tasks as SSE events
    for task in progress["completed_tasks"][last_seen:]:
        yield ChatEvent(type="status", data=f"Completed: {task['task_id']}")
        # Fetch and emit artifact...
    last_seen = len(progress["completed_tasks"])

    if progress["status"] in ("synthesizing", "done"):
        break
    await asyncio.sleep(1)

# Wait for final result
result = await handle.result()
```

This gives users incremental progress (artifacts appear as subtasks finish) without requiring a full event bus.

**3c. Stream synthesis text** (`client.py`, `activities.py`)

For the synthesis phase, consider using the Anthropic streaming API in the activity and storing chunks. The client can poll for new chunks via a Temporal query. Alternatively, accept the fake-chunking for now — synthesis is typically fast (2-5 seconds) so the UX impact is small.

Decision: **defer real synthesis streaming to a future iteration.** Fake-chunking the synthesis is acceptable given the progress polling handles the long-running execution phase.

#### Phase 4: Tests

**4a. Unit tests for models** (`tests/test_temporal_models.py`)

- `ExecutionPlan.batches()` with: no tasks, single task, linear chain, diamond DAG, circular deps
- Serialization round-trip for all dataclasses (verify Temporal's `DataConverter` handles them)

**4b. Activity tests with mocked LLM** (`tests/test_temporal_activities.py`)

Use `ActivityEnvironment` from `temporalio.testing`:

- `plan_subtasks`: mock Anthropic to return valid JSON plan, verify `ExecutionPlan` output
- `plan_subtasks`: mock Anthropic to return malformed JSON, verify error propagates
- `execute_subtask`: mock Anthropic + `execute_code`, verify artifact saved, verify summary format
- `synthesize_results`: mock Anthropic, verify synthesis text returned

**4c. Workflow integration test** (`tests/test_temporal_workflows.py`)

Use `WorkflowEnvironment.start_time_skipping()`:

- Register mock activities that return canned results
- Test simple plan (1 task): verify result has plan, results, synthesis
- Test parallel plan (3 tasks, 2 independent + 1 dependent): verify batching works
- Test partial failure: one mock activity raises, verify other results still present
- Test empty plan: verify graceful handling

**4d. Client integration test** (`tests/test_temporal_client.py`)

Test the `TemporalClient.chat()` generator:

- Verify it yields events in the correct order: status, artifact(s), text chunk(s), done
- Verify conversation history is loaded and passed through
- Verify error handling when Temporal connection fails

#### Phase 5: End-to-End Validation

**5a. Start Temporal dev server**

```bash
just temporal-server
```

Verify containers are running and Temporal UI is accessible at `http://localhost:18233`.

**5b. Start the worker**

```bash
just worker
```

Verify it connects to Temporal and logs "worker started."

**5c. Start the app and test manually**

```bash
just serve
```

Test cases:
1. Select Temporal mode, ask "What's the average age on the Titanic?" — expect single subtask, correct result
2. Ask "Compare survival rates by class and by gender" — expect 2+ parallel subtasks
3. Follow up with "What about by age group?" — expect conversation context to carry through
4. Kill the worker mid-workflow — expect timeout/error, not a hang

**5d. Verify in Temporal UI**

- Open `http://localhost:18233`
- Confirm workflows appear with correct task queue
- Inspect activity inputs/outputs for correctness
- Check retry behavior on any failures

## System-Wide Impact

- **Interaction graph**: Chat request → routes.py selects TemporalClient → starts Temporal workflow → worker picks up → activities call Anthropic API + Monty sandbox → artifacts saved to SQLite → workflow returns → client emits SSE events
- **Error propagation**: Activity errors → Temporal retries (3x) → if still failing, `SubTaskResult` with error → synthesis mentions failure → user sees partial results. Connection errors → caught in client → error SSE event → done event
- **State lifecycle risks**: Artifacts saved during activities could be orphaned if the workflow fails after subtask execution but before the client emits them. Mitigation: conversation_id association ensures they're at least findable. No cleanup needed — orphaned artifacts are harmless.
- **API surface parity**: No other interfaces need changes. The frontend already has the Temporal option. The SSE protocol is identical across modes.

## Acceptance Criteria

### Functional Requirements

- [ ] User can select Temporal mode and get a working response to a simple question
- [ ] Complex questions decompose into parallel subtasks that execute concurrently
- [ ] Follow-up questions have conversation context (planner sees prior messages)
- [ ] Artifacts are associated with the correct conversation and appear in sidebar history
- [ ] Partial failures in subtasks don't crash the whole workflow — user gets partial results
- [ ] User sees progress updates as subtasks complete (not a single long wait)
- [ ] Workflow has a 5-minute overall timeout
- [ ] Worker limits concurrent activities to 5

### Non-Functional Requirements

- [ ] Activities heartbeat every 30 seconds during LLM calls
- [ ] Activity inputs use single-dataclass pattern for forward compatibility
- [ ] Shared Anthropic client across activities (not created per-call)
- [ ] Worker and Temporal server have Justfile commands

### Quality Gates

- [ ] Unit tests for models (batching, serialization)
- [ ] Activity tests with mocked LLM (happy path + error cases)
- [ ] Workflow integration tests with time-skipping environment
- [ ] End-to-end manual test with real Temporal server

## Dependencies & Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Anthropic API rate limits with parallel calls | Medium | Medium | `max_concurrent_activities=5`, exponential backoff in retry policy |
| Temporal dataclass serialization issues | Low | High | Test serialization round-trips in Phase 4a before Phase 1 |
| SQLite concurrent writes from worker + app | Medium | Medium | SQLite WAL mode (verify it's enabled), low activity concurrency |
| DuckDB concurrent reads from parallel activities | Low | Medium | DuckDB handles concurrent reads well; in-memory mode avoids file locking |
| Temporal server not running → workflow hangs | High | Medium | Add connection check in Phase 1e; clear error message |

## Sources & References

### Internal References

- Architecture doc: `docs/temporal-agent-architecture.md`
- Existing client pattern: `src/sandbox_agent/agent/client.py` (AgentClient as reference impl)
- ChatEvent protocol: `src/sandbox_agent/shared.py:19-24`
- Route handler / mode selection: `src/sandbox_agent/api/routes.py:41-48`
- ExecutionResult: `src/sandbox_agent/engine/executor.py:17-23`
- SQLite save_artifact: `src/sandbox_agent/engine/sqlite_store.py:159-168`

### External References

- Temporal Python SDK docs: https://docs.temporal.io/develop/python/core-application
- Activity serialization best practices: https://docs.temporal.io/develop/python/converters-and-encryption
- Testing workflows: https://docs.temporal.io/develop/python/testing-suite
- Async activity patterns: https://docs.temporal.io/develop/python/python-sdk-sync-vs-async
- Error handling: https://docs.temporal.io/develop/python/error-handling
- asyncio.gather in workflows: https://temporal.io/blog/durable-distributed-asyncio-event-loop
