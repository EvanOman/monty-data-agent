"""Pydantic AI agent client — reimplements Standard mode using the Pydantic AI framework."""

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)

from ..agent.prompts import build_system_prompt
from ..config import PYDANTIC_AI_MODEL
from ..engine.functions import ExternalFunctions
from ..shared import ChatEvent, ToolExecutor

logger = logging.getLogger(__name__)


@dataclass
class AgentDeps:
    """Dependencies injected into Pydantic AI tool functions."""

    tool_executor: ToolExecutor
    conversation_id: str
    pending_artifacts: list[dict] = field(default_factory=list)
    tool_timings: list[dict] = field(default_factory=list)
    event_callback: Callable[[str, str], Coroutine[Any, Any, None]] | None = None


def create_agent(system_prompt: str) -> Agent[AgentDeps, str]:
    """Create a Pydantic AI agent with execute_code and load_result tools."""
    agent: Agent[AgentDeps, str] = Agent(
        PYDANTIC_AI_MODEL,
        system_prompt=system_prompt,
        deps_type=AgentDeps,
        end_strategy="exhaustive",
    )

    @agent.tool
    async def execute_code(ctx: RunContext[AgentDeps], code: str) -> str:
        """Execute Python code in the Monty sandbox. The code can call fetch(), count(), describe(), and tables() to access datasets. Returns a result UID and metadata — the full data is rendered to the user automatically."""
        deps = ctx.deps

        if deps.event_callback:
            await deps.event_callback("code", code)

        summary, artifact, timing = await deps.tool_executor.run_code(
            code, deps.conversation_id, on_event=deps.event_callback
        )
        deps.pending_artifacts.append(artifact)
        deps.tool_timings.append(timing)
        return summary

    @agent.tool
    async def load_result(ctx: RunContext[AgentDeps], uid: str) -> str:
        """Load result data into context by its UID. Returns up to 100 rows formatted as a markdown table. Use this when you need to reference specific values in your analysis."""
        deps = ctx.deps
        if deps.event_callback:
            await deps.event_callback("status", "Loading result data...")
        return await deps.tool_executor.load_result(uid)

    return agent


def _build_message_history(history: list[dict]) -> list[ModelMessage]:
    """Convert SQLite message history to Pydantic AI ModelMessage objects."""
    messages: list[ModelMessage] = []
    for msg in history:
        if msg["role"] == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=msg["content"])]))
        elif msg["role"] == "assistant":
            messages.append(ModelResponse(parts=[TextPart(content=msg["content"])]))
    return messages


class PydanticAIClient:
    """Pydantic AI agent client with the same interface as AgentClient."""

    def __init__(self, duckdb_store, sqlite_store) -> None:
        self._duckdb = duckdb_store
        self._sqlite = sqlite_store
        self._ext_functions = ExternalFunctions(duckdb_store)
        self._tool_executor = ToolExecutor(self._ext_functions, sqlite_store)
        self._agent: Agent[AgentDeps, str] | None = None

    def set_schema_context(self, ctx: str) -> None:
        system_prompt = build_system_prompt(ctx)
        self._agent = create_agent(system_prompt)

    async def chat(self, conversation_id: str, user_message: str):
        """Yield ChatEvent objects as the agent processes the message.

        Uses agent.iter() for node-by-node control of the agent loop,
        which correctly handles multi-turn tool calling across all model
        providers (Anthropic, OpenAI, etc.).
        """
        if self._agent is None:
            raise RuntimeError("Must call set_schema_context() before chat()")

        agent = self._agent
        event_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        pending_artifacts: list[dict] = []
        tool_timings: list[dict] = []

        t_chat_start = time.time()

        yield ChatEvent(type="status", data="Starting analysis...")

        history = await self._sqlite.get_messages(conversation_id)
        if history and history[-1]["content"] == user_message:
            history = history[:-1]

        message_history = _build_message_history(history)

        async def event_callback(etype: str, data: str) -> None:
            await event_queue.put((etype, data))

        deps = AgentDeps(
            tool_executor=self._tool_executor,
            conversation_id=conversation_id,
            pending_artifacts=pending_artifacts,
            tool_timings=tool_timings,
            event_callback=event_callback,
        )

        timing_spans: list[dict] = []
        turn_count = 0
        tool_call_count = 0

        async def run_agent():
            """Run the Pydantic AI agent loop node-by-node in a background task."""
            nonlocal turn_count, tool_call_count
            try:
                await event_queue.put(("status", "Agent is thinking..."))

                async with agent.iter(
                    user_message,
                    deps=deps,
                    message_history=message_history,
                ) as run:
                    async for node in run:
                        if Agent.is_model_request_node(node):
                            # Model is generating a response — stream text deltas
                            turn_count += 1
                            t_turn_start = time.time()

                            async with node.stream(run.ctx) as request_stream:
                                async for event in request_stream:
                                    if isinstance(event, PartDeltaEvent):
                                        if isinstance(event.delta, TextPartDelta):
                                            await event_queue.put(
                                                ("text", event.delta.content_delta)
                                            )
                                    elif isinstance(event, FinalResultEvent):
                                        # Model is producing its final text answer;
                                        # stream the remaining text via stream_text
                                        break

                                # Stream any remaining final text
                                async for text in request_stream.stream_text(delta=True):
                                    await event_queue.put(("text", text))

                            t_turn_end = time.time()
                            timing_spans.append(
                                {
                                    "name": f"LLM Turn {turn_count}",
                                    "type": "llm",
                                    "start_ms": round((t_turn_start - t_chat_start) * 1000),
                                    "duration_ms": round((t_turn_end - t_turn_start) * 1000),
                                }
                            )

                        elif Agent.is_call_tools_node(node):
                            # Tools are being executed
                            t_tool_start = time.time()
                            await event_queue.put(("status", "Executing tools..."))

                            async with node.stream(run.ctx) as handle_stream:
                                async for event in handle_stream:
                                    if isinstance(event, FunctionToolCallEvent):
                                        tool_call_count += 1
                                        logger.info(
                                            "Tool call: %s",
                                            event.part.tool_name,
                                        )

                            t_tool_end = time.time()
                            timing_spans.append(
                                {
                                    "name": "Tool Execution",
                                    "type": "tool",
                                    "start_ms": round((t_tool_start - t_chat_start) * 1000),
                                    "duration_ms": round((t_tool_end - t_tool_start) * 1000),
                                }
                            )
                            await event_queue.put(("status", "Analyzing results..."))

            except Exception as e:
                logger.exception("Error in Pydantic AI agent run")
                await event_queue.put(("error", str(e)))
            finally:
                await event_queue.put(("_sentinel", None))

        agent_task = asyncio.create_task(run_agent())

        while True:
            event_type, event_data = await event_queue.get()

            if event_type == "_sentinel":
                break
            elif event_data is not None:
                yield ChatEvent(type=event_type, data=event_data)

        await agent_task

        # Emit artifact events
        for artifact in pending_artifacts:
            artifact_data = {
                "id": artifact["id"],
                "code": artifact["code"],
                "result_json": artifact.get("result_json"),
                "result_type": artifact.get("result_type"),
                "error": artifact.get("error"),
            }
            yield ChatEvent(type="artifact", data=json.dumps(artifact_data))

        t_chat_end = time.time()
        total_ms = round((t_chat_end - t_chat_start) * 1000)

        timing_data = {
            "total_ms": total_ms,
            "turns": turn_count,
            "tool_calls": tool_call_count,
            "spans": timing_spans,
            "tool_details": tool_timings,
        }

        yield ChatEvent(
            type="done",
            data=json.dumps(
                {
                    "artifacts": [a["id"] for a in pending_artifacts],
                    "timing": timing_data,
                }
            ),
        )

    async def close(self) -> None:
        pass
