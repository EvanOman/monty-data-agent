# Chatkit: Reusable Chat UI Library

**Date:** 2026-04-01
**Status:** Brainstorm

## What We're Building

A reusable, frontend-focused chat UI library called **chatkit** that extracts the common chat patterns from sandbox-agent, highlight_helper, and future projects into a standalone package. The library uses **Web Components** for framework-agnostic reusability and defines a clean **SSE event protocol** as the backend integration contract, with a small **Python helpers package** for FastAPI integration.

## Why This Approach

Evan has re-implemented the same chat UI pattern at least 3 times across projects. The core pattern is identical each time:
- SSE-over-POST with manual `ReadableStream` parsing
- Sidebar with conversation/thread list
- Streaming message rendering with markdown
- User/assistant message bubbles
- Dark mode, responsive layout

By extracting this into Web Components with a well-defined event protocol, any backend that speaks the protocol gets the full chat UI for free.

## Key Decisions

1. **Web Components** (not vanilla JS or a framework) — framework-agnostic, native browser standard, encapsulated styling, importable as ES modules
2. **Frontend-focused with backend contract** — the library owns the UI; backends just need to emit the right SSE events
3. **SSE event protocol + Python helpers** — event type definitions plus a small Python package with Pydantic models and FastAPI SSE formatting helpers
4. **Kitchen sink, all configurable** — include tool cards, artifact rendering, diagnostics, model picker, coaching cards, etc. Everything is opt-in via attributes/slots/configuration
5. **Name: chatkit** — lives at `/home/evan/dev/chatkit` as a sibling project

## Research Findings

### Shared Patterns Across Projects (extraction targets)

| Pattern | sandbox-agent | highlight_helper |
|---------|--------------|-----------------|
| SSE-over-POST parsing | Manual `ReadableStream` + `\n\n` split | Same, nearly identical code |
| Message bubbles | User (right, blue) / Assistant (left, gray) | Same layout and colors |
| Sidebar | Conversation list + new chat button | Thread list + new chat button |
| Dark mode | CSS custom properties + class toggle | Tailwind dark: variants + class toggle |
| Markdown rendering | `marked` + highlight.js (re-render each chunk) | `streaming-markdown` (smd, incremental) |
| Styling | Tailwind CDN + custom CSS | Tailwind CDN + custom CSS |
| Backend | FastAPI + Pydantic + SQLAlchemy/SQLite | Same stack |

### SSE Event Types (union of both projects)

| Event | sandbox-agent | highlight_helper | Proposed chatkit |
|-------|--------------|-----------------|-----------------|
| `init` | `{conversation_id}` | — | `{thread_id}` |
| `text` | streaming text chunks | default (no event name) | `text` |
| `status` | "Thinking...", "Running code..." | — | `status` |
| `code` | code about to execute | — | `code` |
| `tool_use` | — | `{tool, input}` | `tool_use` |
| `tool_done` | — | `{tool, summary}` | `tool_done` |
| `artifact` | JSON artifact payload | — | `artifact` |
| `error` | error text | error text | `error` |
| `done` | `{timing?}` | thread_id | `done` |

### Unique Features to Include

- **Tool use cards** (highlight_helper): animated spinner during execution, result summary
- **Artifact cards** (sandbox-agent): tabbed display with Table/Value/Code/Raw JSON/Error panels
- **Diagnostics waterfall** (sandbox-agent): timing visualization with LLM vs tool spans
- **Model picker** (highlight_helper): dropdown to switch LLM model
- **Coaching cards** (highlight_helper): proactive engagement cards linked to threads
- **Mode selector** (sandbox-agent): dropdown to switch agent/orchestration mode
- **Code blocks** (sandbox-agent): collapsible code execution display

## Proposed Architecture

```
chatkit/
  src/
    components/           # Web Components
      chat-app.js         # Top-level orchestrator (<chat-app>)
      chat-sidebar.js     # Conversation list (<chat-sidebar>)
      chat-messages.js    # Message container (<chat-messages>)
      chat-message.js     # Single message bubble (<chat-message>)
      chat-input.js       # Input bar with send button (<chat-input>)
      chat-tool-card.js   # Tool use/done indicator (<chat-tool-card>)
      chat-artifact.js    # Tabbed artifact display (<chat-artifact>)
      chat-diagnostics.js # Timing waterfall (<chat-diagnostics>)
      chat-code-block.js  # Collapsible code block (<chat-code-block>)
    streaming/
      sse-client.js       # SSE-over-POST client (the shared ReadableStream parser)
      event-types.js      # Event type constants and payload shapes
    theme/
      styles.css          # Base styles, CSS custom properties, dark mode
      tailwind-preset.js  # Tailwind preset for chatkit colors/spacing
    index.js              # Public API entry point
  python/
    chatkit/
      events.py           # Pydantic models for all SSE event payloads
      sse.py              # FastAPI SSE helper functions (sse_text, sse_done, etc.)
      protocols.py        # Protocol/ABC for chat backends
  examples/
    minimal/              # Bare-bones example
    with-tools/           # Example with tool use cards
    full-featured/        # Kitchen sink demo
```

## Integration Contract

A backend integrates with chatkit by:

1. **Implementing a POST endpoint** that returns `text/event-stream`
2. **Emitting SSE events** matching the chatkit protocol (text, done, error, and optionally tool_use, artifact, etc.)
3. **Optionally using the Python helpers** (`from chatkit import sse_text, sse_done, ChatEvent`) for type-safe event emission

A frontend integrates by:

1. **Importing the Web Components** (`<script type="module" src="chatkit/index.js">`)
2. **Placing `<chat-app>`** in the page with configuration attributes
3. **Configuring the endpoint URL** and enabled features via attributes or properties

## Resolved Questions

1. **Markdown library**: Use `streaming-markdown` (smd) — purpose-built for token-by-token rendering, more efficient for streaming use case.
2. **Conversation persistence API**: Define SSE + REST contract — specify expected endpoints for thread CRUD (GET /threads, DELETE /threads/:id, etc.) so the sidebar works out of the box. Python helpers include Pydantic models for these.
3. **CSS strategy**: Ship as a Tailwind preset/plugin. Matches Evan's current workflow across all projects. Host app must use Tailwind.
4. **Packaging**: TBD during planning — likely monorepo with JS + Python packages.
