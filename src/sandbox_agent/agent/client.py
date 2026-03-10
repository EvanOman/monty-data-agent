import asyncio
import json
import logging
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from ..config import MAX_AGENT_TURNS, MODEL
from ..engine.functions import ExternalFunctions
from ..shared import ChatEvent, ToolExecutor
from .prompts import build_system_prompt

logger = logging.getLogger(__name__)


class AgentClient:
    """Routes user messages through Claude Agent SDK with Monty/DuckDB tool execution."""

    def __init__(self, duckdb_store, sqlite_store) -> None:
        self._duckdb = duckdb_store
        self._sqlite = sqlite_store
        self._ext_functions = ExternalFunctions(duckdb_store)
        self._tool_executor = ToolExecutor(self._ext_functions, sqlite_store)
        self._schema_context: str = ""

    def set_schema_context(self, ctx: str) -> None:
        self._schema_context = ctx

    def _make_tools(self):
        tool_executor = self._tool_executor

        @tool(
            "execute_code",
            "Execute Python code in the Monty sandbox. The code can call fetch(), count(), describe(), and tables() to access datasets. Returns a result UID and metadata — the full data is rendered to the user automatically.",
            {"code": str},
        )
        async def execute_code_tool(args: dict) -> dict:
            code = args["code"]

            async def on_event(etype: str, data: str) -> None:
                if self._event_queue:
                    await self._event_queue.put((etype, data))

            summary, artifact, timing = await tool_executor.run_code(
                code, self._current_conversation_id, on_event=on_event
            )
            self._pending_artifacts.append(artifact)
            self._tool_timings.append(timing)
            return {"content": [{"type": "text", "text": summary}]}

        @tool(
            "load_result",
            "Load result data into context by its UID. Returns up to 100 rows formatted as a markdown table. Use this when you need to reference specific values in your analysis.",
            {"uid": str},
        )
        async def load_result_tool(args: dict) -> dict:
            text = await tool_executor.load_result(args["uid"])
            return {"content": [{"type": "text", "text": text}]}

        return [execute_code_tool, load_result_tool]

    def _build_mcp_server(self):
        return create_sdk_mcp_server(
            name="sandbox",
            version="1.0.0",
            tools=self._make_tools(),
        )

    def _build_prompt_with_history(self, user_message: str, history: list[dict]) -> str:
        if not history:
            return user_message
        parts = []
        for msg in history:
            role = msg["role"].capitalize()
            parts.append(f"{role}: {msg['content']}")
        parts.append(f"User: {user_message}")
        return "\n\n".join(parts)

    async def chat(self, conversation_id: str, user_message: str):
        """Yield ChatEvent objects as the agent processes the message.

        Uses an asyncio.Queue so tool handlers can push real-time status
        events during execution, rather than blocking until the entire
        agent loop completes.
        """
        self._current_conversation_id = conversation_id
        self._pending_artifacts = []
        self._tool_timings = []
        self._event_queue = asyncio.Queue()

        t_chat_start = time.time()

        # Yield immediately so the user sees something right away
        yield ChatEvent(type="status", data="Starting analysis...")

        history = await self._sqlite.get_messages(conversation_id)
        if history and history[-1]["content"] == user_message:
            history = history[:-1]

        prompt = self._build_prompt_with_history(user_message, history)

        mcp_server = self._build_mcp_server()
        system_prompt = build_system_prompt(self._schema_context)

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=MODEL,
            mcp_servers={"sandbox": mcp_server},
            allowed_tools=["mcp__sandbox__execute_code", "mcp__sandbox__load_result"],
            max_turns=MAX_AGENT_TURNS,
        )

        timing_spans = []
        turn_count = 0
        tool_call_count = 0
        last_span_time = time.time()

        async def run_agent():
            """Run the agent loop in a background task, pushing events to the queue."""
            nonlocal turn_count, tool_call_count, last_span_time
            try:
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(prompt)
                    await self._event_queue.put(("status", "Agent is thinking..."))

                    async for message in client.receive_response():
                        now = time.time()

                        if isinstance(message, AssistantMessage):
                            turn_count += 1
                            timing_spans.append(
                                {
                                    "name": f"LLM Turn {turn_count}",
                                    "type": "llm",
                                    "start_ms": round((last_span_time - t_chat_start) * 1000),
                                    "duration_ms": round((now - last_span_time) * 1000),
                                }
                            )
                            last_span_time = now

                            for block in message.content:
                                if (
                                    isinstance(block, TextBlock)
                                    and block.text
                                    and block.text.strip()
                                ):
                                    await self._event_queue.put(("text", block.text))
                                elif isinstance(block, ToolUseBlock):
                                    tool_call_count += 1
                                    if block.name == "mcp__sandbox__execute_code":
                                        code = block.input.get("code", "")
                                        await self._event_queue.put(("code", code))
                                    elif block.name == "mcp__sandbox__load_result":
                                        await self._event_queue.put(
                                            ("status", "Loading result data...")
                                        )

                        elif isinstance(message, ResultMessage):
                            now2 = time.time()
                            timing_spans.append(
                                {
                                    "name": "Tool Execution",
                                    "type": "tool",
                                    "start_ms": round((last_span_time - t_chat_start) * 1000),
                                    "duration_ms": round((now2 - last_span_time) * 1000),
                                }
                            )
                            last_span_time = now2
                            await self._event_queue.put(("status", "Analyzing results..."))

            except Exception as e:
                logger.exception("Error in agent run")
                await self._event_queue.put(("error", str(e)))
            finally:
                await self._event_queue.put(("_sentinel", None))

        # Launch agent as a background task so its events flow in real-time
        agent_task = asyncio.create_task(run_agent())

        # Consume events from the queue and yield them as ChatEvents
        while True:
            event_type, event_data = await self._event_queue.get()

            if event_type == "_sentinel":
                break
            elif event_type == "text":
                yield ChatEvent(type="text", data=event_data)
            elif event_type == "status":
                yield ChatEvent(type="status", data=event_data)
            elif event_type == "code":
                yield ChatEvent(type="code", data=event_data)
            elif event_type == "error":
                yield ChatEvent(type="error", data=event_data)

        await agent_task

        # After streaming is done, emit artifact events
        for artifact in self._pending_artifacts:
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
            "tool_details": self._tool_timings,
        }

        yield ChatEvent(
            type="done",
            data=json.dumps(
                {
                    "artifacts": [a["id"] for a in self._pending_artifacts],
                    "timing": timing_data,
                }
            ),
        )

    async def close(self) -> None:
        pass
