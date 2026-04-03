---
title: "refactor: Extract reusable chat UI into chatkit library"
type: refactor
status: active
date: 2026-04-01
origin: docs/brainstorms/2026-04-01-chatkit-brainstorm.md
---

# Extract Reusable Chat UI into chatkit Library

## Enhancement Summary

**Deepened on:** 2026-04-01
**Research agents used:** 10 (best-practices, framework-docs, architecture, security, performance, frontend-races, simplicity, python, patterns, typescript)

### Key Improvements
1. **Simplified from 10 to 7 components, 6 to 3 phases** — YAGNI analysis cut diagnostics and merged thin components
2. **DOMPurify is mandatory** — LLM output is untrusted; streaming-markdown alone is not safe
3. **State machine replaces boolean `isStreaming`** — prevents 8 identified race conditions
4. **TypeScript with async iterator SSE API** — typed library with modern streaming patterns
5. **StrEnum + factory classmethods for Python package** — replaces loose string types and per-event helper functions
6. **Protocol versioning from day one** — `protocol_version` in `init` event prevents future pain

### New Considerations Discovered
- Shadow DOM + Tailwind v4 `@property` requires hoisting rules to document scope
- `streaming-markdown` needs rAF batching to avoid layout thrash on long responses
- SSE parser must use O(n) tail-tracking instead of O(n^2) buffer re-scanning
- Origin-lock the `onBeforeFetch` callback to prevent token leakage
- Web Component `disconnectedCallback` cleanup is critical — use a CkBase class with automatic teardown

---

## Overview

Extract the chat frontend from sandbox-agent (and patterns shared with highlight_helper) into a standalone, reusable Web Components library called **chatkit**. The library provides a complete streaming chat UI that any Python/FastAPI backend can integrate with by implementing a well-defined SSE event protocol. Includes a small Python helpers package for type-safe event emission.

## Problem Statement / Motivation

The same chat UI has been re-implemented at least 3 times across projects (sandbox-agent, highlight_helper, and others). Each implementation copies ~1000 lines of nearly identical code: SSE-over-POST parsing, message bubble rendering, sidebar thread management, dark mode, streaming markdown. Bug fixes and improvements in one project don't flow to others. New projects start by copy-pasting and modifying, accumulating drift.

## Proposed Solution

Create `/home/evan/dev/chatkit/` as a sibling git repository containing:

1. **TypeScript Web Components** — `<ck-app>`, `<ck-sidebar>`, `<ck-messages>`, `<ck-input>`, etc. with Shadow DOM encapsulation
2. **SSE client module** — async-iterator-based `ReadableStream` parser extracted into a reusable ES module
3. **CSS theme** — `chatkit.css` with CSS custom properties for full dark/light theming, plus optional Tailwind v4 `@theme` integration
4. **Python `chatkit` package** — Pydantic models for all event payloads, FastAPI SSE helper functions, and a Protocol class for chat backends
5. **Protocol specification** — documented inline in types and README (event types, payload schemas, REST endpoints, ordering constraints)

sandbox-agent then imports chatkit instead of maintaining its own inline chat UI.

## Technical Approach

### Component Architecture

Prefix all components with `ck-` to avoid collisions (see brainstorm: resolved question on naming).

```html
<ck-app api-base="/api">
  <ck-sidebar slot="sidebar"></ck-sidebar>
  <ck-messages slot="messages"></ck-messages>
  <ck-input slot="input"></ck-input>
</ck-app>
```

**Component inventory (7 components):**

| Component | Responsibility | Key attributes/properties |
|-----------|---------------|--------------------------|
| `<ck-app>` | Orchestrator, SSE client, state machine | `api-base`, `theme` |
| `<ck-sidebar>` | Thread list, new chat, delete with confirmation | Receives thread list from `<ck-app>` |
| `<ck-messages>` | Message container, turn grouping, auto-scroll, status indicator | Auto-scrolls, manages turn boundaries |
| `<ck-message>` | Single message bubble (user or assistant) with streaming markdown | `role`, `content`, streaming state |
| `<ck-input>` | Text input + send button + stop button | `placeholder`, `disabled` during streaming |
| `<ck-tool-card>` | Tool use/done indicator with spinner | `tool-name`, `status` (running/done), `summary` |
| `<ck-artifact>` | Tabbed rich card (generalized from sandbox-agent artifacts) | `data` (JSON), configurable tab structure |

### Research Insights: Component Simplification

**Merged or cut from v1 (per simplicity + architecture reviews):**
- `<ck-status>` absorbed into `<ck-messages>` — a pulsing-dot indicator is ~20 lines of DOM, not worth a full Web Component
- `<ck-code-block>` absorbed into `<ck-message>` — collapsible code is a rendering concern within a message
- `<ck-diagnostics>` cut from v1 — timing waterfall is a developer tool specific to sandbox-agent; extract later if a second consumer needs it
- Turn grouping handled internally by `<ck-messages>` via a `.ck-turn` container div

**Events are the feature toggles** — no explicit feature flag system needed. If the backend sends `tool_use` events, tool cards appear. If it doesn't, they don't. Components render when their events arrive.

### Research Insights: Shadow DOM Decision

**Use Shadow DOM with adopted stylesheets.** Despite the simplicity reviewer's suggestion of light DOM, the best-practices research and multi-instance requirement favor Shadow DOM:

- Prevents style leakage between chatkit and host page CSS
- Makes `querySelector` instance-scoped by default (critical for multiple `<ck-app>` instances)
- `adoptedStyleSheets` eliminates per-instance CSS parsing overhead (100x improvement per performance review)
- CSS custom properties (`--ck-*`) pierce shadow boundaries for theming

**Tailwind v4 integration via `@theme`:**
```css
/* chatkit.css — consumers import this */
@import "tailwindcss";

@theme {
  --color-ck-bg: oklch(0.12 0 0);
  --color-ck-surface: oklch(0.16 0 0);
  --color-ck-text: oklch(0.93 0 0);
  --color-ck-accent: oklch(0.65 0.18 250);
  --color-ck-user-bubble: oklch(0.45 0.18 250);
  --color-ck-assistant-bubble: oklch(0.20 0 0);
}

/* Dark/light via class on <html> */
[data-theme="light"] {
  --color-ck-bg: oklch(0.98 0 0);
  --color-ck-surface: oklch(0.95 0 0);
  --color-ck-text: oklch(0.15 0 0);
}
```

Inside Shadow DOM, components use adopted stylesheets with `:root` rewritten to `:host`. `@property` rules are hoisted to document scope (per framework-docs research on Tailwind v4 + Shadow DOM).

### Research Insights: TypeScript + Build Tooling

**Use TypeScript** (per JS/TS review). A reusable library needs typed attributes, events, and properties. Ship `.js` + `.d.ts` + source maps.

**Vite library mode** for builds (ESM-only, `es2022` target):
```typescript
// vite.config.ts
export default defineConfig({
  build: {
    lib: {
      entry: {
        index: 'src/index.ts',
        register: 'src/register.ts',
        'sse/client': 'src/sse/sse-client.ts',
      },
      formats: ['es'],
    },
    rollupOptions: {
      external: ['streaming-markdown', 'dompurify'],
    },
    target: 'es2022',
  },
});
```

**Separate class definition from registration:**
```typescript
// src/components/ck-app.ts — exports class, NO side effects
export class CkApp extends CkBase { ... }

// src/register.ts — side-effect module
import { CkApp } from './components/ck-app.js';
if (!customElements.get('ck-app')) customElements.define('ck-app', CkApp);
```

**Bundle size target:** <20KB gzipped for the full library.

### CkBase: Reactive Element with Automatic Cleanup

All chatkit components extend a thin base class (per performance + races reviews):

```typescript
abstract class CkBase extends HTMLElement {
  static properties: Record<string, PropertyDeclaration> = {};

  #cleanups: (() => void)[] = [];
  #updateRequested = false;

  /** Register a cleanup function — runs in disconnectedCallback */
  protected addCleanup(fn: () => void): void { this.#cleanups.push(fn); }

  /** Add event listener with automatic cleanup */
  protected listen(target: EventTarget, event: string, handler: EventListener, options?: AddEventListenerOptions): void {
    target.addEventListener(event, handler, options);
    this.#cleanups.push(() => target.removeEventListener(event, handler, options));
  }

  /** Batched updates via queueMicrotask */
  protected requestUpdate(): void {
    if (this.#updateRequested) return;
    this.#updateRequested = true;
    queueMicrotask(() => {
      this.#updateRequested = false;
      this.update();
    });
  }

  protected abstract update(): void;

  disconnectedCallback(): void {
    for (const fn of this.#cleanups) fn();
    this.#cleanups.length = 0;
  }
}
```

### SSE Protocol Specification

**Required events (every backend must emit):**

| Event | Payload | Description |
|-------|---------|-------------|
| `init` | `{"thread_id": "<string>", "protocol_version": 1}` | First event; provides thread ID and protocol version |
| `text` | Raw string (markdown delta) | Streamed token-by-token; accumulated and rendered incrementally |
| `done` | `{"timing?": {...}, "artifacts?": [...]}` | Last event; signals stream complete |
| `error` | Raw string (error message) | Error during processing; may terminate stream |

**Optional events:**

| Event | Payload | Description |
|-------|---------|-------------|
| `status` | Raw string | Status message ("Thinking...", "Running code...") |
| `code` | Raw string (source code) | Code preview before execution |
| `tool_use` | `{"tool": "<name>", "input": {...}}` | Tool invocation started |
| `tool_done` | `{"tool": "<name>", "summary": "<string>"}` | Tool invocation completed |
| `artifact` | `{"id": "<string>", "type": "<string>", "data": {...}}` | Rich structured result |

**Event ordering contract:**
1. `init` MUST be the first event
2. `done` or `error` MUST be the last event
3. `text` events are **deltas** (appended, not replacements) — consumers accumulate
4. `tool_use` opens a tool card; `tool_done` closes it — they must be paired
5. `status` events replace previous status (only one visible at a time)
6. Unknown event types MUST be silently ignored (forward compatibility)

**REST endpoints (conversation CRUD):**

| Method | Path | Required? | Description |
|--------|------|-----------|-------------|
| `POST` | `/chat` | Yes | Send message, returns SSE stream |
| `GET` | `/conversations` | Optional | List conversations (for sidebar) |
| `GET` | `/conversations/{id}` | Optional | Load conversation with messages |
| `DELETE` | `/conversations/{id}` | Optional | Delete a conversation |

All paths are relative to `api-base`. Request body for `/chat`:
```json
{"thread_id": "<string|null>", "message": "<string>", "metadata": {}}
```

The `metadata` field is pass-through for app-specific data (e.g., sandbox-agent's `mode`, highlight_helper's `book_id`).

### Research Insights: SSE Client — Async Iterator API

Replace callback-based `connectSSE()` with an async-iterator-based API (per JS/TS review):

```typescript
interface SSEConnection {
  [Symbol.asyncIterator](): AsyncIterableIterator<SSEEvent>;
  abort(): void;
  readonly done: Promise<void>;
}

function connectSSE(url: string, options?: SSEOptions): SSEConnection;

// Usage in <ck-app>:
const sse = connectSSE(`${this.apiBase}/chat`, {
  body: { thread_id, message, metadata },
  signal: this.abortController.signal,
  headers: await this.getHeaders(),
});

try {
  for await (const event of sse) {
    this.handleEvent(event);
  }
  this.finalize('complete');
} catch (err) {
  if (err.name !== 'AbortError') this.finalize('error', err);
}
```

**SSE parser optimization** (per performance review — O(n) instead of O(n^2)):
```typescript
// Track only the incomplete tail, not the full history
const text = tail + value;
const lastNewline = text.lastIndexOf('\n\n');
if (lastNewline === -1) { tail = text; continue; }
const complete = text.substring(0, lastNewline);
tail = text.substring(lastNewline + 2);
```

Use `TextDecoderStream` (built-in, handles streaming UTF-8 correctly) piped from `response.body`.

### Research Insights: Stream Lifecycle State Machine

Replace boolean `isStreaming` with a state machine (per frontend races review):

```typescript
const enum StreamState {
  IDLE = 'idle',
  SENDING = 'sending',       // fetch in flight, no SSE yet
  STREAMING = 'streaming',   // SSE events arriving
  FINALIZING = 'finalizing', // done received, cleaning up
}

// Transitions:
// IDLE → SENDING (user sends message)
// SENDING → STREAMING (first SSE event arrives)
// SENDING → IDLE (fetch fails)
// STREAMING → FINALIZING (done event)
// FINALIZING → IDLE (cleanup complete)
// ANY → IDLE (abort / disconnect / error)
```

**`finalize()` is idempotent** — first caller wins, second caller is a no-op. This prevents the abort-vs-done race condition.

**Double abort-signal check** around `reader.read()`:
```typescript
if (signal.aborted) break;
const { done, value } = await reader.read();
if (signal.aborted) break; // component could disconnect during read
```

### Research Insights: Security — DOMPurify is Mandatory

**CRITICAL** (per security review): LLM output is untrusted content. Prompt injection, RAG poisoning, or multi-tenant scenarios can produce malicious markdown.

```typescript
import DOMPurify from 'dompurify';

// Single sanitizing code path for ALL HTML insertion
private setMessageHTML(html: string): void {
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ['p','strong','em','code','pre','ul','ol','li','a','h1','h2','h3','h4','h5','h6','blockquote','table','thead','tbody','tr','th','td','br','hr','details','summary','span','div'],
    ALLOWED_ATTR: ['href','class','open'],
    ALLOW_DATA_ATTR: false,
  });
  this.messageContainer.innerHTML = clean;
}
```

- **DOMPurify is bundled** (not CDN) — no external dependency at the sanitization layer
- **Allowlist `href` schemes** — only `https:`, `http:`, `mailto:`, `#`
- **All non-message UI** uses `textContent`, never `innerHTML`

### Research Insights: Streaming Performance

**Batch smd updates with `requestAnimationFrame`** (per performance review):
```typescript
private pendingText = '';
private rafScheduled = false;

appendStreamingText(chunk: string): void {
  this.pendingText += chunk;
  if (!this.rafScheduled) {
    this.rafScheduled = true;
    requestAnimationFrame(() => {
      smd.parser_write(this.parser, this.pendingText);
      this.pendingText = '';
      this.rafScheduled = false;
      this.scheduleScroll();
    });
  }
}
```

This collapses 3-10 chunks per frame into 1 DOM update. Expected: 3-10x fewer layouts during fast streaming.

**Auto-scroll with rAF coalescing** — use a sentinel element + `scrollIntoView({ block: 'end', behavior: 'instant' })`, batched via rAF. Detect "user scrolled away" via passive scroll listener with 50px threshold.

**`content-visibility: auto`** on off-screen messages to skip rendering cost.

### Research Insights: Authentication — Origin-Lock

**Origin-lock the `onBeforeFetch` callback** (per security review):
```typescript
widget.onBeforeFetch = ({ url, origin }) => {
  if (origin !== 'https://api.myapp.com') return {};
  return { Authorization: `Bearer ${token}` };
};
```

Provide an optional `allowedOrigins` property on `<ck-app>` as a hard gate.

### DOM Event Table

(Per pattern recognition review — define all custom events formally)

| Event | `bubbles` | `composed` | `cancelable` | `detail` type |
|-------|-----------|------------|--------------|---------------|
| `ck-before-send` | true | true | true | `{ message: string, metadata: Record<string, unknown> }` |
| `ck-stream-start` | true | true | false | `{ threadId: string }` |
| `ck-stream-end` | true | true | false | `{ threadId: string, reason: 'complete' \| 'aborted' \| 'error' }` |
| `ck-error` | true | true | false | `{ message: string, source: 'network' \| 'parse' \| 'server' }` |
| `ck-thread-select` | true | true | false | `{ threadId: string }` |
| `ck-thread-delete` | true | true | true | `{ threadId: string }` |

`ck-before-send` is cancelable — `e.preventDefault()` aborts the send. Consumers can modify `e.detail.metadata` to inject app-specific fields.

### Python Helpers Package

```
chatkit/
  pyproject.toml
  src/
    chatkit/
      __init__.py       # Re-exports public API
      events.py         # ChatEvent dataclass + ChatEventType StrEnum
      models.py         # Pydantic models: ChatRequest, SSEPayload
      sse.py            # FastAPI SSE formatting (thin wrapper)
      protocols.py      # ChatBackend Protocol
      py.typed          # PEP 561 marker
```

### Research Insights: Python Package Design

**StrEnum for event types** (per Python review — prevents silent typos):
```python
class ChatEventType(StrEnum):
    INIT = "init"
    TEXT = "text"
    STATUS = "status"
    CODE = "code"
    TOOL_USE = "tool_use"
    TOOL_DONE = "tool_done"
    ARTIFACT = "artifact"
    ERROR = "error"
    DONE = "done"

@dataclass(slots=True, frozen=True)
class ChatEvent:
    type: ChatEventType
    data: str = ""

    @classmethod
    def text(cls, content: str) -> "ChatEvent":
        return cls(type=ChatEventType.TEXT, data=content)

    @classmethod
    def error(cls, message: str) -> "ChatEvent":
        return cls(type=ChatEventType.ERROR, data=message)

    # Factory classmethods for each event type — replaces sse_text(), sse_done(), etc.
```

**Fix Protocol signature** — drop `async`, use explicit `metadata` param:
```python
@runtime_checkable
class ChatBackend(Protocol):
    def chat(
        self,
        thread_id: str | None,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncGenerator[ChatEvent, None]: ...
```

**SSEPayload model** — single point of serialization:
```python
class SSEPayload(BaseModel):
    event: ChatEventType
    data: str

    @classmethod
    def from_chat_event(cls, event: ChatEvent) -> "SSEPayload":
        return cls(event=event.type, data=event.data)
```

**`src/` layout** with `py.typed` marker and `__init__.py` re-exports. Use `Field(default_factory=dict)` on `ChatRequest.metadata`.

### Configuration & Extension Points

**Two extension points for v1** (per simplicity review — `ck-before-send` and `artifactRenderer` are cut):

1. **`onBeforeFetch`** — callback for auth header injection (origin-locked)
2. **`metadata`** — pass-through field on `ChatRequest` for app-specific data

Additional extension via the DOM event table above — consumers can listen for lifecycle events and react accordingly.

**Stream cancellation — stop button:**
Built-in "Stop generating" button appears during streaming. Uses `AbortController` to cancel the fetch. Backend should detect disconnect via `request.is_disconnected()`.

## Implementation Phases

### Phase 1: Core — SSE Client + Components + Theme

**Deliverables:**
- Initialize `/home/evan/dev/chatkit/` git repo with TypeScript + Vite library mode
- `package.json` with Vite, TypeScript, DOMPurify, streaming-markdown deps
- `pyproject.toml` for the Python helpers package (uv-managed, `src/` layout)
- `CkBase` class with reactive properties, cleanup registration, queueMicrotask batching
- SSE client module: async iterator API, O(n) parser, `TextDecoderStream`, `AbortController`
- `<ck-message>` — user and assistant bubbles with streaming markdown (smd) + DOMPurify sanitization + rAF batching
- `<ck-input>` — text input, send button, stop button (appears during streaming)
- `<ck-messages>` — message container with turn grouping, auto-scroll (sentinel + rAF), status indicator, `content-visibility: auto`
- `<ck-tool-card>` — spinner during execution, summary on completion
- `<ck-artifact>` — generalized tabbed card with configurable tab structure
- CSS theme: `chatkit.css` with `--ck-*` custom properties, dark/light variants, optional Tailwind v4 `@theme`
- Theme toggle support (respects `prefers-color-scheme`, listens for OS changes via `matchMedia`)
- Python package: `ChatEvent` (StrEnum + frozen dataclass + factory classmethods), `ChatBackend` Protocol, `ChatRequest` model, `SSEPayload` model, `py.typed`
- Stream lifecycle state machine (IDLE → SENDING → STREAMING → FINALIZING → IDLE)
- Unit tests: SSE client (all event types, abort, edge cases), Python helpers (event formatting, model validation)
- Separate class definition from registration (`src/register.ts` for auto-define)

**Success criteria:**
- Can connect to sandbox-agent's `/api/chat` and render a streaming conversation
- DOMPurify sanitizes all rendered markdown
- State machine prevents all identified race conditions
- Stop button cancels active stream
- Dark/light theme toggle works
- Python helpers can format events that the SSE client parses
- Tests pass; bundle <20KB gzipped

### Phase 2: Full UI — Sidebar + CRUD + Polish

**Deliverables:**
- `<ck-sidebar>` — thread list, new chat button, delete with confirmation, responsive drawer on mobile
- `<ck-app>` — top-level orchestrator wiring sidebar, messages, and input via Shadow DOM slots
- REST client for conversation CRUD (list, load, delete) with proper error states
- Conversation state management (encapsulated per `<ck-app>` instance, not global)
- Empty states for sidebar (no conversations) and messages (new conversation)
- Mobile responsive layout: sidebar as slide-out drawer below `md:` breakpoint
- DOM event table implementation (all 6 events from the table above)
- `onBeforeFetch` callback with origin-lock
- Conversation delete: abort stream before delete, optimistic sidebar update
- `README.md` with quick-start guide, event table, and API reference
- `examples/minimal/` — bare-bones chat with mock SSE server

**Success criteria:**
- Can list, create, load, and delete conversations against sandbox-agent's API
- Two `<ck-app>` instances on the same page operate independently
- Mobile drawer layout works
- All DOM events fire correctly with proper `composed`/`cancelable` behavior
- README is sufficient for integration in <30 minutes

### Phase 3: Migration — sandbox-agent Integration

**Deliverables:**
- sandbox-agent adds chatkit as dependency (local path)
- Replace `static/index.html` inline chat with chatkit Web Components import
- Map sandbox-agent's existing SSE events to chatkit protocol:
  - `ChatEvent` imported from chatkit (replaces local `ChatEvent` in `shared.py`)
  - SSE helpers replaced by `SSEPayload.from_chat_event()` (replaces `sse.py` functions)
  - Routes updated to use chatkit's `ChatRequest` model with `metadata.mode`
  - Rename `conversation_id` → `thread_id` across the codebase
- Add `tool_use`/`tool_done` events to sandbox-agent's agent pipeline
- sandbox-agent-specific customizations:
  - Mode selector in a slot
  - Custom artifact renderer for data-analysis table/scalar/dict views
  - Dataset hint text
- Add conversation DELETE endpoint to sandbox-agent
- Verify all 7 agent modes still work end-to-end

**Success criteria:**
- sandbox-agent's chat UI looks and behaves identically to the current implementation
- All 7 modes produce correct output with streaming
- No regression in artifact rendering or code blocks
- `static/index.html` reduced from ~1062 lines to <100 (imports + config + app-specific slots)

## Alternative Approaches Considered

1. **React component library** — Rejected because all existing projects use vanilla JS/Jinja2 templates. React would add a large dependency and framework lock-in. (see brainstorm)

2. **Lit Web Components** — Considered as a thin layer over native Web Components for templating/reactivity. Deferred — start with vanilla `CkBase` class (~80 lines), add Lit later if boilerplate becomes painful. The best-practices research recommends Lit for 10+ component libraries, but chatkit has 7 with a thin base class that covers the core reactive patterns.

3. **Light DOM instead of Shadow DOM** — Simplicity reviewer advocated this. Rejected because: (a) multiple instances need scoped `querySelector`, (b) style leakage from host pages is a real risk for a reusable library, (c) `adoptedStyleSheets` eliminates the CSS parsing overhead concern. CSS custom properties handle theming across the boundary.

4. **Full-stack kit with backend routes** — Rejected in favor of frontend-focused library with protocol contract. Each project has different persistence needs. (see brainstorm)

5. **SSE + adapter pattern for sidebar** — Considered callbacks/adapters instead of a REST contract. User chose "SSE + REST contract" so the sidebar works out of the box. (see brainstorm)

## System-Wide Impact

### Interaction Graph

When `<ck-app>` renders:
1. Sidebar fetches `GET {api-base}/conversations` to populate thread list
2. User sends message → `<ck-app>` dispatches `ck-before-send` event (cancelable) → calls `onBeforeFetch` for headers → POSTs to `{api-base}/chat`
3. State machine transitions: IDLE → SENDING → STREAMING (on first event)
4. SSE async iterator yields events → `<ck-app>` routes to child components via property pushing
5. On `done` event → state machine: STREAMING → FINALIZING → IDLE; dispatches `ck-stream-end`; optimistic sidebar update
6. On error → `<ck-app>` dispatches `ck-error`; renders error in messages area; state → IDLE

### Error Propagation

- HTTP non-2xx from chat endpoint → SSE client rejects, `<ck-app>` transitions to IDLE, renders error
- Malformed SSE event → SSE client logs warning, skips event, continues stream
- Network disconnect mid-stream → fetch rejects, `<ck-app>` shows "Connection lost" status
- Backend `error` event → renders error bubble, stream may continue or end
- Component rendering failure → try/catch in every component's render path, fallback UI for broken components

### State Lifecycle Risks

- **Orphaned streaming state**: `disconnectedCallback` aborts the `AbortController`, cleaning up the stream. The `CkBase` class runs all registered cleanup functions.
- **Multiple instances**: Each `<ck-app>` owns its own state machine, abort controller, and shadow DOM. No shared globals, no shared localStorage keys.
- **Abort-vs-done race**: `finalize()` is idempotent — first caller wins. Both abort and done funnel through the same method.
- **Delete during streaming**: Abort stream FIRST, clear messages, THEN send DELETE. (per races review)

### API Surface Parity

sandbox-agent and highlight_helper both need migration. The protocol is a superset of both:
- sandbox-agent: needs `tool_use`/`tool_done` events added, `conversation_id` renamed to `thread_id`
- highlight_helper: needs `init` event added, default text event renamed to `text`, thread management aligned with chatkit REST contract

## Acceptance Criteria

### Functional Requirements

- [ ] `<ck-app>` renders a complete chat UI with sidebar, messages, and input
- [ ] SSE async iterator connects via POST and parses all protocol event types
- [ ] Streaming markdown renders incrementally using `streaming-markdown` (smd) with rAF batching
- [ ] All rendered HTML passes through DOMPurify before DOM insertion
- [ ] Dark/light theme toggle works, respects system preference, listens for OS changes
- [ ] Sidebar lists conversations, creates new ones, deletes with confirmation
- [ ] Tool cards show spinner during execution and summary on completion
- [ ] Artifact cards display tabbed rich content
- [ ] Stop button cancels active stream via `AbortController`
- [ ] `onBeforeFetch` callback enables origin-locked auth header injection
- [ ] `metadata` field on chat requests enables app-specific data pass-through
- [ ] Stream lifecycle state machine prevents all identified race conditions
- [ ] sandbox-agent migrated to import chatkit with no visual/behavioral regression
- [ ] All 6 custom DOM events fire correctly per event table specification

### Non-Functional Requirements

- [ ] Two `<ck-app>` instances on the same page work independently
- [ ] Mobile responsive layout with drawer sidebar below `md:` breakpoint
- [ ] Components use Shadow DOM with `adoptedStyleSheets` for style encapsulation
- [ ] CSS custom properties (`--ck-*`) enable theme customization across shadow boundaries
- [ ] Python package installable via `uv add` from local path, includes `py.typed` marker
- [ ] JS package importable as ES module with full `.d.ts` type definitions
- [ ] Bundle size <20KB gzipped
- [ ] SSE parser uses O(n) tail-tracking, not O(n^2) buffer re-scanning

### Quality Gates

- [ ] Unit tests for SSE client (all event types, edge cases, abort, UTF-8 boundaries)
- [ ] Unit tests for Python helpers (event formatting, model validation, StrEnum)
- [ ] Integration test: chatkit components against sandbox-agent backend
- [ ] README with quick-start, event table, and API reference
- [ ] `disconnectedCallback` cleanup verified — no memory leaks on conversation switching

## Dependencies & Prerequisites

- **Before Phase 1**: None — greenfield project
- **Before Phase 3**: Phases 1-2 complete; sandbox-agent's conversation DELETE endpoint added
- **After Phase 3**: Consider migrating highlight_helper as second consumer

## Risk Analysis & Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| Shadow DOM + Tailwind v4 `@property` | `@property` rules don't work in shadow roots | Hoist `@property` rules to document scope at build time; CSS custom properties work fine |
| streaming-markdown (smd) limitations | May not handle all markdown edge cases | Test against sandbox-agent's actual LLM output; smd supports all common markdown features; fallback to marked.js if needed |
| DOMPurify stripping needed elements | Over-sanitization of legitimate LLM output | Carefully tuned allowlist; test against real LLM responses from all 7 modes |
| Web Component boilerplate overhead | Vanilla WC can be verbose for reactive UIs | `CkBase` class handles 90% of boilerplate (reactive properties, cleanup, batched updates); add Lit if it becomes painful |
| Migration breaks existing functionality | sandbox-agent's 7 modes are complex | Phase 3 includes mode-by-mode verification; keep old `index.html` as fallback during migration |
| Two-package maintenance burden | JS + Python packages to keep in sync | StrEnum + TypeScript types are the source of truth; SSEPayload model validates both sides |
| XSS via LLM output | Prompt injection, RAG poisoning | DOMPurify on ALL HTML; single sanitizing code path; `textContent` for non-message UI |

## Sources & References

### Origin

- **Brainstorm document:** [docs/brainstorms/2026-04-01-chatkit-brainstorm.md](../brainstorms/2026-04-01-chatkit-brainstorm.md) — Key decisions: Web Components, frontend-focused with SSE protocol contract, Python helpers package, kitchen sink features, Tailwind preset, streaming-markdown.

### Internal References

- sandbox-agent chat frontend: `static/index.html` (1062 lines)
- sandbox-agent SSE helpers: `src/sandbox_agent/api/sse.py:1-39`
- sandbox-agent ChatEvent: `src/sandbox_agent/shared.py:19-24`
- sandbox-agent API models: `src/sandbox_agent/api/models.py:1-33`
- sandbox-agent API routes: `src/sandbox_agent/api/routes.py:26-91`
- highlight_helper chat template: `/home/evan/dev/highlight_helper/app/templates/chat.html` (~797 lines)
- highlight_helper SSE streaming: `/home/evan/dev/highlight_helper/app/api/chat.py:360-441`

### External References

- [streaming-markdown (smd)](https://github.com/thetarnav/streaming-markdown) — incremental markdown renderer (~3KB)
- [DOMPurify](https://github.com/cure53/DOMPurify) — HTML sanitizer
- [Tailwind CSS v4 Functions and Directives](https://tailwindcss.com/docs/functions-and-directives) — `@theme` CSS-first config
- [Shoelace/Web Awesome theming](https://shoelace.style/getting-started/customizing) — CSS custom property + `::part()` patterns
- [sse-starlette](https://github.com/sysid/sse-starlette) — FastAPI SSE library
- [MDN: adoptedStyleSheets](https://developer.mozilla.org/en-US/docs/Web/API/Document/adoptedStyleSheets)
- [Open WC Testing](https://open-wc.org/guides/developing-components/testing/) — `@web/test-runner` for WC unit tests
- [Vite Library Mode](https://vite.dev/config/build-options) — ESM output for component libraries

### Spec Flow Analysis Gaps Addressed

- Gap 6 (stream cancellation): AbortController + stop button in Phase 1
- Gap 7 (no DELETE endpoint): Added to Phase 3 prerequisites
- Gap 10 (sidebar error handling): Proper error states in Phase 2
- Gap 11 (HTTP status checking): Fixed in SSE client (`if (!res.ok)`)
- Gap 14 (base URL config): `api-base` attribute on `<ck-app>`
- Gap 15 (authentication): `onBeforeFetch` callback with origin-lock
- Gap 19 (global state): State machine + Shadow DOM scoping per instance
- Gap 20 (history replay ordering): Addressed in Phase 2 conversation loading
- Gap 23 (mobile layout): Responsive drawer in Phase 2
