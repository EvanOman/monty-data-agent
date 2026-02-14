# Data Analysis Chat Agent — Design Notes

## What This Is

An experimental sandbox for exploring patterns in building AI-powered data analysis agents.
The system routes natural language questions through Claude, which generates Python code
executed in a restricted sandbox (Pydantic Monty), with results displayed as interactive
artifact cards in a streaming chat UI.

The whole purpose is to play with different ideas for agent architecture — tool design,
sandbox execution, result rendering, streaming UX, and diagnostics. Not a production app.

## Architecture

```
Browser (HTML/Tailwind/SSE)
    ↕ HTTP + SSE (POST-based, not EventSource)
FastAPI (root_path="/sandbox-agent", port 19876)
    ↕
Claude Agent SDK (ClaudeSDKClient, MCP tools)
    ↕ asyncio.Queue-based streaming
Monty Sandbox (Rust-based Python subset, external function pause/resume)
    ↕
DuckDB (in-memory, 6 pre-loaded calmcode.io datasets)
    ↕
SQLite (conversations, messages, code artifacts with Monty bytecode)
```

## Key Design Decisions

### Python-First Analysis (not SQL)
The agent was originally given a raw `sql()` function but wrote complex SQL statements that
weren't representative of the target problem (mix of API calls + Python analysis). We replaced
`sql()` with constrained data access functions:

- `fetch(table, columns, where, order_by, limit)` — equality-only WHERE filters
- `count(table, where)` — simple row counts
- `describe(table_name)` — column metadata
- `tables()` — list available tables

This forces the agent to do computation in Python (loops, aggregations, transformations)
rather than pushing everything into SQL. The `where` parameter only supports equality filters,
making anything complex require Python code.

### asyncio.Queue Streaming Architecture
The original implementation had `receive_response()` blocking the entire async generator,
so nothing reached the SSE stream until the full agent loop completed. The fix:

- Agent runs as `asyncio.create_task(run_agent())` pushing events to an `asyncio.Queue`
- Tool handlers push real-time status events mid-execution ("Running code in sandbox...")
- Chat generator consumes from queue and yields `ChatEvent` objects
- Immediate `yield ChatEvent(type="status", data="Starting analysis...")` before anything else

This gives users a dynamic streaming experience — they see what the agent is thinking,
what code it's writing, and when tools are executing.

### Artifact System
Every code execution produces a durable artifact stored in SQLite:
- `id` (UUID), `code`, `result_json`, `result_type`, `error`
- `monty_state` (serialized VM bytecode for exact replay)
- Artifacts are emitted as SSE events after the agent loop completes
- Frontend renders them as tabbed cards: Table/Result | Code | Raw JSON | Error

The agent sees only a summary of each result (UID + type + row count/column names),
not the raw data. It can use `load_result` to pull specific values if needed.

### Result Classification
`_classify_output()` in executor.py determines the result type:
- `list[dict]` → `"table"` (rendered as HTML data table)
- `dict` → `"dict"` (rendered as pre-formatted JSON)
- `int/float/str/bool` → `"scalar"` (rendered as plain value)
- Anything else → `"other"` (rendered as JSON string)
- `None` → `"none"` (no result panel)

The frontend only renders proper HTML tables for `result_type === "table"`. Everything
else gets a `<pre>` block with JSON. **This is a known gap** — scalars, dicts, and lists
of mixed types all render as raw JSON instead of something more meaningful.

### Timing Diagnostics
OTel-style spans tracked without a collector:
- Spans between `AssistantMessage` and `ResultMessage` yields (LLM turns vs tool execution)
- Tool execution timing inside handlers
- Rendered as a collapsible panel with color-coded timeline bar (indigo=LLM, amber=tool)
- Included in the `done` SSE event payload

### SSE Event Types
| Event | Data | Purpose |
|-------|------|---------|
| `init` | `{conversation_id}` | Assigns/confirms conversation |
| `status` | string | Real-time status updates (thinking, running, analyzing) |
| `text` | markdown string | Agent prose, rendered with marked.js |
| `code` | Python string | Code preview (replaced by artifact card) |
| `artifact` | JSON `{id, code, result_json, result_type, error}` | Execution result card |
| `error` | string | Error display |
| `done` | JSON `{artifacts, timing}` | Completion signal with diagnostics |

### Frontend
Single HTML file, no build step:
- Tailwind CDN for styling (dark mode: bg-gray-900)
- marked.js v12 for markdown rendering (with `safeMarkdown()` fallback wrapper)
- highlight.js for code syntax highlighting
- Manual SSE parsing via `fetch()` + `ReadableStream` (POST-based, can't use EventSource)
- `\r\n` normalization in parser for cross-platform compatibility

## File Structure

```
src/sandbox_agent/
├── config.py              — Settings (port 19876, model, limits)
├── main.py                — FastAPI app + lifespan (startup/shutdown)
├── data/
│   ├── datasets.py        — 6 calmcode.io datasets (titanic, bigmac, smoking, stocks, pokemon, stigler)
│   ├── duckdb_store.py    — DuckDB in-memory, dataset loading, schema introspection
│   └── sqlite_store.py    — Conversations, messages, artifacts tables
├── sandbox/
│   ├── executor.py        — Monty compile + start/resume loop + result classification
│   └── functions.py       — External function handlers (fetch, count, describe, tables)
├── agent/
│   ├── client.py          — Queue-based streaming, MCP tools (execute_code, load_result), timing
│   └── prompts.py         — System prompt with schema context + Monty constraints
└── api/
    ├── routes.py           — POST /api/chat (SSE), GET /api/conversations, replay endpoint
    ├── sse.py              — SSE formatting helpers
    └── models.py           — Pydantic request/response models

static/index.html          — Complete chat UI
tests/                     — executor, duckdb_store, API tests (17 passing)
```

## Deployment

Run locally with `just serve`, or deploy as a systemd user service. Set `ROOT_PATH`
environment variable if serving behind a reverse proxy on a subpath.

## Datasets
6 pre-loaded from calmcode.io via DuckDB httpfs:
- **titanic** (714 rows) — passenger survival
- **bigmac** (1,330 rows) — Big Mac Index
- **smoking** (1,314 rows) — Simpson's paradox
- **stocks** (4,276 rows) — MSFT/KLM/ING/MOS prices
- **pokemon** (800 rows) — name/type/total/hp/attack
- **stigler** (77 rows) — diet optimization
