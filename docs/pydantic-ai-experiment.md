# Pydantic AI Agent Experiment

*March 2025 — Adding model-portable agent support to sandbox-agent*

> **Disclaimer:** This is an informal experiment, not a scientific benchmark. Results are from single runs on a shared development machine. They give a general indication of performance but are not reproducible or statistically meaningful.

## Background

The sandbox-agent project is a data analysis tool with a chat interface, a DuckDB backend, and a sandboxed Python executor (Monty). It originally had two operating modes:

- **Standard** — Claude Agents SDK with tool calling via an in-process MCP server
- **Code Mode** — Raw Anthropic API with an out-of-process MCP server subprocess

Both are locked to Anthropic models. We added a third mode — **Pydantic AI** — to get model portability while keeping the same user-facing behavior.

## What We Built

A new `pydantic_agent/` module that implements the same `chat()` async generator interface using Pydantic AI's `Agent` class. The key refactoring step was extracting shared tool logic (sandbox execution, artifact storage, result formatting) into a `ToolExecutor` class in `shared.py`, so both agent implementations delegate to the same core.

## Eval Results

Prompt: *"Count the rows in the titanic table"* (expected answer: 714)

| Configuration | Runtime | Turns | Tool Calls | Result | Correct |
|---|---|---|---|---|---|
| Standard (Claude SDK + Sonnet 4.5) | 9,056 ms | 3 | 1 | 714 | Yes |
| Pydantic AI + Claude Sonnet 4.5 | 8,985 ms | 2 | 1 | 714 | Yes |
| Pydantic AI + GPT 5.4 | 4,014 ms | 2 | 1 | 714 | Yes |
| Code Mode (Raw API + MCP subprocess) | 18,846 ms | 4 | 3 | 714 | Yes |

All four configurations got the correct answer. GPT 5.4 was the fastest by a wide margin, likely due to faster token generation rather than any framework difference. Code Mode was the slowest because it spawns a subprocess for the MCP server and the raw API loop generated redundant tool calls (search before execute).

The Standard and Pydantic AI modes with the same model (Sonnet 4.5) performed nearly identically — the framework overhead is negligible.

## Code Comparison: Claude Agents SDK vs Pydantic AI

### Tool Definition

**Claude Agents SDK** — Manual schema, untyped `args` dict:

```python
@tool(
    "execute_code",
    "Execute Python code in the Monty sandbox...",
    {"code": str},
)
async def execute_code_tool(args: dict) -> dict:
    code = args["code"]
    summary, artifact, timing = await tool_executor.run_code(
        code, self._current_conversation_id, on_event=on_event
    )
    self._pending_artifacts.append(artifact)
    self._tool_timings.append(timing)
    return {"content": [{"type": "text", "text": summary}]}
```

**Pydantic AI** — Typed function signature, auto-generated schema:

```python
@agent.tool
async def execute_code(ctx: RunContext[AgentDeps], code: str) -> str:
    deps = ctx.deps
    if deps.event_callback:
        await deps.event_callback("code", code)
    summary, artifact, timing = await deps.tool_executor.run_code(
        code, deps.conversation_id, on_event=deps.event_callback
    )
    deps.pending_artifacts.append(artifact)
    deps.tool_timings.append(timing)
    return summary
```

Pydantic AI generates the JSON schema from the function signature and docstring. No manual `{"code": str}` mapping, no wrapping return values in `{"content": [{"type": "text", "text": ...}]}`. Just return a string.

### Dependency Injection

**Claude Agents SDK** — No DI system. Tool closures capture `self` from the enclosing class:

```python
def _make_tools(self):
    tool_executor = self._tool_executor

    @tool("execute_code", ...)
    async def execute_code_tool(args: dict) -> dict:
        # Accesses self._event_queue, self._pending_artifacts via closure
        ...
```

**Pydantic AI** — First-class `deps_type` with `RunContext`:

```python
@dataclass
class AgentDeps:
    tool_executor: ToolExecutor
    conversation_id: str
    pending_artifacts: list[dict] = field(default_factory=list)
    tool_timings: list[dict] = field(default_factory=list)
    event_callback: Callable | None = None

agent = Agent(model, deps_type=AgentDeps)

@agent.tool
async def execute_code(ctx: RunContext[AgentDeps], code: str) -> str:
    deps = ctx.deps  # Typed, explicit
```

The deps pattern makes it clear what data tools can access and keeps the agent definition stateless.

### Conversation History

**Claude Agents SDK** — No native history support. We manually flatten history into the prompt:

```python
def _build_prompt_with_history(self, user_message, history):
    parts = []
    for msg in history:
        role = msg["role"].capitalize()
        parts.append(f"{role}: {msg['content']}")
    parts.append(f"User: {user_message}")
    return "\n\n".join(parts)
```

This concatenates everything into a single string, losing the structured turn boundaries that models can leverage.

**Pydantic AI** — Native `message_history` parameter:

```python
message_history = _build_message_history(history)  # -> list[ModelMessage]

async with self._agent.iter(
    user_message,
    deps=deps,
    message_history=message_history,  # Structured turns
) as run:
    ...
```

Each turn remains a typed `ModelRequest` or `ModelResponse` object with proper role boundaries.

### Streaming & Agent Loop

**Claude Agents SDK** — Message-level streaming. You get complete `AssistantMessage` and `ResultMessage` objects:

```python
async with ClaudeSDKClient(options=options) as client:
    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    await queue.put(("text", block.text))
```

No token-level deltas — you get the full text block at once.

**Pydantic AI** — Token-level streaming with node-based loop control via `iter()`:

```python
async with self._agent.iter(user_message, deps=deps, ...) as run:
    async for node in run:
        if Agent.is_model_request_node(node):
            async with node.stream(run.ctx) as stream:
                async for event in stream:
                    if isinstance(event, PartDeltaEvent):
                        if isinstance(event.delta, TextPartDelta):
                            await queue.put(("text", event.delta.content_delta))
        elif Agent.is_call_tools_node(node):
            async with node.stream(run.ctx) as handle_stream:
                async for event in handle_stream:
                    ...
```

More verbose, but gives fine-grained control over each phase of the agent loop. Text streams token-by-token while the user watches.

### Model Portability

**Claude Agents SDK** — Anthropic models only.

**Pydantic AI** — Provider-prefixed model strings:

```python
PYDANTIC_AI_MODEL = "openai:gpt-5.4"       # OpenAI
PYDANTIC_AI_MODEL = "anthropic:claude-sonnet-4-5-20250929"  # Anthropic
```

Same agent code, same tools, same streaming — just change the model string.

## Gotchas We Hit

### OpenAI models co-locate text + tool calls

GPT 5.4 sends text and tool call parts in the same response. With Pydantic AI's default `end_strategy="early"`, the text was treated as the final result and tool calls were silently skipped:

```
ToolReturnPart: content='Tool not executed - a final result was already processed.'
```

**Fix:** Set `end_strategy="exhaustive"` on the Agent constructor.

### `run_stream` doesn't handle multi-turn tool loops well

Even with `end_strategy="exhaustive"`, using `run_stream` + `stream_text(delta=True)` hung after the first tool round. The streamer only captured text from the initial model response, not subsequent responses after tool results.

**Fix:** Switch to the `iter()` API which provides explicit node-by-node control (ModelRequestNode for text, CallToolsNode for tools) and handles multi-turn correctly across all providers.

## Summary

Pydantic AI provides a cleaner developer experience — typed tools, explicit DI, native history, model portability. The tradeoffs are a more verbose streaming setup and less mature documentation for advanced patterns like `iter()`.

For this project, we're defaulting to **Pydantic AI + GPT 5.4** since it was the fastest configuration and the code is cleaner. The Standard (Claude SDK) and Code Mode options remain available via the dropdown.
