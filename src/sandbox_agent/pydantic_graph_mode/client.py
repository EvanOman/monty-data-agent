"""Pydantic Graph client — Plan-Execute-Synthesize using pydantic-graph beta API.

Uses GraphBuilder with @g.step, .map() fan-out, and .join() fan-in for
type-safe parallel subtask execution. All subtasks in a plan run concurrently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic
from pydantic_graph.beta import GraphBuilder, StepContext
from pydantic_graph.beta.join import reduce_list_append

from ..config import PYDANTIC_GRAPH_MODEL as MODEL
from ..engine.executor import execute_code
from ..engine.functions import ExternalFunctions
from ..planning import SYNTHESIZE_SYSTEM_PROMPT, build_plan_prompt, build_subtask_prompt
from ..planning.helpers import (
    chunk_text,
    format_history_prompt,
    format_result_summary,
    parse_plan_json,
    strip_code_fences,
)
from ..planning.models import SubTask, SubTaskResult
from ..shared import ChatEvent

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10


@dataclass
class GraphDeps:
    """Dependencies passed through the graph via StepContext."""

    anthropic: AsyncAnthropic
    plan_system_prompt: str
    subtask_system_prompt: str
    ext_functions: ExternalFunctions
    sqlite_store: Any
    conversation_id: str
    user_message: str
    conversation_history: str
    event_queue: asyncio.Queue[tuple[str, str | None]]
    pending_artifacts: list[dict] = field(default_factory=list)


class PydanticGraphClient:
    """Plan-Execute-Synthesize client using pydantic-graph beta API."""

    def __init__(self, duckdb_store, sqlite_store) -> None:
        self._duckdb = duckdb_store
        self._sqlite = sqlite_store
        self._ext_functions = ExternalFunctions(duckdb_store)
        self._anthropic = AsyncAnthropic()
        self._schema_context: str = ""

    def set_schema_context(self, ctx: str) -> None:
        self._schema_context = ctx

    async def chat(self, conversation_id: str, user_message: str):
        """Yield ChatEvent objects as the graph processes the message."""
        t_start = time.time()

        yield ChatEvent(type="status", data="Planning analysis...")

        # Load conversation history
        history = await self._sqlite.get_messages(conversation_id)
        if history and history[-1]["content"] == user_message:
            history = history[:-1]
        history = history[-MAX_HISTORY_MESSAGES:]
        history_text = format_history_prompt(history)

        event_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        deps = GraphDeps(
            anthropic=self._anthropic,
            plan_system_prompt=build_plan_prompt(self._schema_context),
            subtask_system_prompt=build_subtask_prompt(self._schema_context),
            ext_functions=self._ext_functions,
            sqlite_store=self._sqlite,
            conversation_id=conversation_id,
            user_message=user_message,
            conversation_history=history_text,
            event_queue=event_queue,
        )

        # Run graph in background, stream events via queue
        async def run_graph():
            try:
                result = await _build_and_run_graph(deps)
                await event_queue.put(("_result", result))
            except Exception as e:
                logger.exception("Error in pydantic-graph pipeline")
                await event_queue.put(("error", str(e)))
            finally:
                await event_queue.put(("_sentinel", None))

        graph_task = asyncio.create_task(run_graph())

        final_text = None
        while True:
            event_type, event_data = await event_queue.get()
            if event_type == "_sentinel":
                break
            elif event_type == "_result":
                final_text = event_data
            elif event_data is not None:
                yield ChatEvent(type=event_type, data=event_data)

        await graph_task

        # Stream synthesis text
        if final_text:
            for text_chunk in chunk_text(final_text):
                yield ChatEvent(type="text", data=text_chunk)

        # Emit artifact events
        artifact_ids = []
        for artifact in deps.pending_artifacts:
            artifact_ids.append(artifact["id"])
            yield ChatEvent(
                type="artifact",
                data=json.dumps(
                    {
                        "id": artifact["id"],
                        "code": artifact["code"],
                        "result_json": artifact.get("result_json"),
                        "result_type": artifact.get("result_type"),
                        "error": artifact.get("error"),
                    }
                ),
            )

        t_end = time.time()
        total_ms = round((t_end - t_start) * 1000)

        yield ChatEvent(
            type="done",
            data=json.dumps(
                {
                    "artifacts": artifact_ids,
                    "timing": {
                        "total_ms": total_ms,
                        "turns": len(deps.pending_artifacts) + 2,
                        "tool_calls": len(deps.pending_artifacts),
                        "mode": "pydantic_graph_mode",
                        "spans": [],
                    },
                }
            ),
        )

    async def close(self) -> None:
        pass


async def _build_and_run_graph(deps: GraphDeps) -> str:
    """Build and execute the Plan-Execute-Synthesize graph. Returns synthesis text."""

    g: GraphBuilder[None, GraphDeps, str, str] = GraphBuilder(
        deps_type=GraphDeps,
        input_type=str,
        output_type=str,
    )

    @g.step
    async def plan(ctx: StepContext[None, GraphDeps, str]) -> list[SubTask]:
        """Decompose the user question into independent subtasks."""
        d = ctx.deps
        await d.event_queue.put(("status", "Decomposing question into subtasks..."))

        history_prefix = d.conversation_history if d.conversation_history else ""
        response = await d.anthropic.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=d.plan_system_prompt,
            messages=[{"role": "user", "content": f"{history_prefix}{ctx.inputs}"}],
        )

        execution_plan = parse_plan_json(response.content[0].text)
        n = len(execution_plan.tasks)
        await d.event_queue.put(("status", f"Plan: {n} subtask{'s' if n != 1 else ''} to execute"))
        return execution_plan.tasks

    @g.step
    async def execute_subtask(ctx: StepContext[None, GraphDeps, SubTask]) -> SubTaskResult:
        """Execute a single subtask: generate code, run in sandbox."""
        d = ctx.deps
        subtask = ctx.inputs
        await d.event_queue.put(("status", f"Executing: {subtask.description[:60]}..."))

        response = await d.anthropic.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=d.subtask_system_prompt,
            messages=[{"role": "user", "content": f"## Task\n{subtask.description}"}],
        )

        code = strip_code_fences(response.content[0].text)
        await d.event_queue.put(("code", code))

        result = await asyncio.to_thread(execute_code, code, d.ext_functions)

        artifact = await d.sqlite_store.save_artifact(
            conversation_id=d.conversation_id,
            message_id=None,
            code=code,
            monty_state=result.monty_state,
            result_json=result.output_json,
            result_type=result.output_type,
            error=result.error,
        )
        d.pending_artifacts.append(artifact)

        if result.error:
            return SubTaskResult(
                task_id=subtask.task_id,
                artifact_uid=artifact["id"],
                summary=f"Error: {result.error}",
                result_type="error",
                error=result.error,
            )

        summary = format_result_summary(artifact["id"], result)
        return SubTaskResult(
            task_id=subtask.task_id,
            artifact_uid=artifact["id"],
            summary=summary,
            result_type=result.output_type,
        )

    join = g.join(reduce_list_append, initial_factory=list)

    @g.step
    async def synthesize(ctx: StepContext[None, GraphDeps, list[SubTaskResult]]) -> str:
        """Combine all subtask results into a coherent response."""
        d = ctx.deps
        results = ctx.inputs
        await d.event_queue.put(("status", "Synthesizing results..."))

        parts = [f"## Original Question\n{d.user_message}\n", "## Analysis Results\n"]
        for r in results:
            status = "SUCCESS" if r.error is None else "FAILED"
            parts.append(f"### {r.task_id}\nStatus: {status}\n{r.summary}\n")

        response = await d.anthropic.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYNTHESIZE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
        return response.content[0].text

    # Wire: start → plan → map(execute) → join → synthesize → end
    g.add_edge(g.start_node, plan)
    g.add_mapping_edge(plan, execute_subtask)
    g.add_edge(execute_subtask, join)
    g.add_edge(join, synthesize)
    g.add_edge(synthesize, g.end_node)

    graph = g.build()
    return await graph.run(deps=deps, inputs=deps.user_message)
